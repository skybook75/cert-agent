/* 브라우저용 수집 Agent 엔진 — PoC scan_to_db.py(load_certs·to_record)와 같은 규칙
 * - 형식 판별: .pfx/.p12 → PKCS#12, PEM 인증서 헤더 → 다건, 그 밖의 PEM → '인증서 아님', 나머지 → DER
 * - 14개 항목: 소유자·발급자(CN/DN), 발급일·만료일, 일련번호, 버전, 공개키, 서명 알고리즘, 종류, SAN, SHA-256 지문
 * - PKCS#12: PBES2(PBKDF2 + AES-CBC)는 WebCrypto로 해독, 인증서 가방만 읽고 개인키는 열지 않음
 * 파일은 브라우저 메모리에서만 처리하며 어디에도 전송하지 않는다.
 */
(function (root) {
  "use strict";
  const subtle = (root.crypto || globalThis.crypto).subtle;
  const EXTS = [".pem", ".crt", ".cer", ".der", ".pfx", ".p12"];
  class CertError extends Error {}

  // ───────── DER
  function node(b, p) {
    if (p + 2 > b.length) throw new CertError("ASN.1 해석 실패(데이터가 짧음)");
    const tag = b[p]; let len = b[p + 1], hl = 2;
    if (len & 0x80) { const n = len & 0x7f; if (n === 0 || n > 4 || p + 2 + n > b.length) throw new CertError("ASN.1 해석 실패(길이 형식)"); len = 0; for (let i = 0; i < n; i++) len = len * 256 + b[p + 2 + i]; hl = 2 + n; }
    const vs = p + hl, end = vs + len;
    if (end > b.length) throw new CertError("ASN.1 해석 실패(데이터가 짧음)");
    return { tag, cons: !!(tag & 0x20), start: p, vs, end, b };
  }
  function kids(n) { const out = []; let p = n.vs; while (p < n.end) { const c = node(n.b, p); out.push(c); p = c.end; } return out; }
  const val = (n) => n.b.subarray(n.vs, n.end);
  const raw = (n) => n.b.subarray(n.start, n.end);
  function oid(n) {
    const v = val(n), parts = [Math.floor(v[0] / 40), v[0] % 40]; let x = 0;
    for (let i = 1; i < v.length; i++) { x = x * 128 + (v[i] & 0x7f); if (!(v[i] & 0x80)) { parts.push(x); x = 0; } }
    if (v[0] >= 80) { parts[0] = 2; parts[1] = v[0] - 80; }
    return parts.join(".");
  }
  const hex = (u8) => Array.from(u8, (x) => x.toString(16).padStart(2, "0")).join("").toUpperCase();
  const td = new TextDecoder("utf-8");
  function str(n) {
    const v = val(n);
    if (n.tag === 0x1e) { let s = ""; for (let i = 0; i + 1 < v.length; i += 2) s += String.fromCharCode((v[i] << 8) | v[i + 1]); return s; }   // BMPString
    if (n.tag === 0x1c) { let s = ""; for (let i = 0; i + 3 < v.length; i += 4) s += String.fromCodePoint((v[i] << 24) | (v[i + 1] << 16) | (v[i + 2] << 8) | v[i + 3]); return s; }
    if (n.tag === 0x14 || n.tag === 0x13 || n.tag === 0x16) return Array.from(v, (c) => String.fromCharCode(c)).join("");   // Teletex/Printable/IA5 → latin1
    return td.decode(v);
  }
  function time(n) {
    const s = Array.from(val(n), (c) => String.fromCharCode(c)).join("");
    let y, rest;
    if (n.tag === 0x17) { y = +s.slice(0, 2); y += y < 50 ? 2000 : 1900; rest = s.slice(2); } else { y = +s.slice(0, 4); rest = s.slice(4); }
    const [mo, d, h, mi, se] = [0, 2, 4, 6, 8].map((i) => rest.slice(i, i + 2) || "00");
    return `${y}-${mo}-${d} ${h}:${mi}:${se}`;
  }

  // ───────── 이름 (cryptography rfc4514_string 과 같은 형식)
  const ATTR = { "2.5.4.3": "CN", "2.5.4.7": "L", "2.5.4.8": "ST", "2.5.4.10": "O", "2.5.4.11": "OU", "2.5.4.6": "C", "2.5.4.9": "STREET",
    "0.9.2342.19200300.100.1.25": "DC", "0.9.2342.19200300.100.1.1": "UID" };
  function esc4514(s) {
    let o = s.replace(/[\\,+"<>;]/g, (c) => "\\" + c);
    if (/^[# ]/.test(o)) o = "\\" + o;
    if (/ $/.test(o) && o.length > 1) o = o.slice(0, -1) + "\\ ";
    return o.replace(/\u0000/g, "\\00");
  }
  function name(n) {
    const rdns = kids(n).map((set) => kids(set).map((atv) => { const [o, v] = kids(atv); return { oid: oid(o), node: v }; }));
    const attrs = rdns.flat();
    const dn = rdns.slice().reverse().map((rdn) => rdn.map((a) => ATTR[a.oid] ? `${ATTR[a.oid]}=${esc4514(str(a.node))}` : `${a.oid}=${esc4514(str(a.node))}`).join("+")).join(",");
    const first = (o) => { const a = attrs.find((x) => x.oid === o); return a ? str(a.node) : null; };
    return { dn, cn: first("2.5.4.3") ?? first("2.5.4.11") ?? first("2.5.4.10") ?? dn };
  }

  // ───────── 인증서 → 14개 항목 (to_record 와 같음)
  const SIG = { "1.2.840.113549.1.1.11": "sha256WithRSAEncryption", "1.2.840.113549.1.1.12": "sha384WithRSAEncryption", "1.2.840.113549.1.1.13": "sha512WithRSAEncryption",
    "1.2.840.113549.1.1.14": "sha224WithRSAEncryption", "1.2.840.113549.1.1.5": "sha1WithRSAEncryption", "1.2.840.113549.1.1.4": "md5WithRSAEncryption", "1.2.840.113549.1.1.10": "RSASSA-PSS",
    "1.2.840.10045.4.3.2": "ecdsa-with-SHA256", "1.2.840.10045.4.3.3": "ecdsa-with-SHA384", "1.2.840.10045.4.3.4": "ecdsa-with-SHA512", "1.2.840.10045.4.1": "ecdsa-with-SHA1",
    "1.3.101.112": "ed25519", "1.3.101.113": "ed448", "1.2.840.10040.4.3": "dsa-with-sha1", "2.16.840.1.101.3.4.3.2": "dsa-with-sha256" };
  const CURVE = { "1.2.840.10045.3.1.7": "secp256r1", "1.3.132.0.34": "secp384r1", "1.3.132.0.35": "secp521r1", "1.3.132.0.10": "secp256k1", "1.2.840.10045.3.1.1": "secp192r1", "1.3.132.0.33": "secp224r1" };
  function bits(intNode) { const v = val(intNode); let i = 0; while (i < v.length - 1 && v[i] === 0) i++; let b = (v.length - i) * 8, x = v[i]; for (let m = 0x80; m && !(x & m); m >>= 1) b--; return b; }
  function keyText(spki) {
    const [alg, bs] = kids(spki), [ao, ap] = kids(alg), o = oid(ao);
    try {
      if (o === "1.2.840.113549.1.1.1") { const k = node(bs.b.subarray(bs.vs + 1, bs.end), 0); return `RSA ${bits(kids(k)[0])}bit`; }
      if (o === "1.2.840.10045.2.1") return CURVE[oid(ap)] ? `EC ${CURVE[oid(ap)]}` : "지원하지 않는 알고리즘";
      if (o === "1.2.840.10040.4.1") return `DSA ${bits(kids(ap)[0])}bit`;
      if (o === "1.3.101.112") return "Ed25519PublicKey";
      if (o === "1.3.101.113") return "Ed448PublicKey";
    } catch { /* 아래 */ }
    return "지원하지 않는 알고리즘";
  }
  async function toRecord(der, fileName) {
    const c = node(der, 0), [tbs, sigAlg] = kids(c);
    let t = kids(tbs), i = 0, version = "v1";
    if (t[0].tag === 0xa0) { const v = val(kids(t[0])[0]); version = v[v.length - 1] === 2 ? "v3" : v[v.length - 1] === 1 ? "v2" : "v1"; i = 1; }
    const serialV = val(t[i]); const serial = hex(serialV).replace(/^0+(?=.)/, "");
    const issuer = name(t[i + 2]), [nb, na] = kids(t[i + 3]), subject = name(t[i + 4]), spki = t[i + 5];
    let san = "", ca = false;
    const extW = t.slice(i + 6).find((x) => x.tag === 0xa3);
    if (extW) for (const e of kids(kids(extW)[0])) {
      const ek = kids(e), eo = oid(ek[0]), ev = ek[ek.length - 1];
      try {
        if (eo === "2.5.29.19") { const s = kids(node(val(ev), 0)); ca = !!(s[0] && s[0].tag === 0x01 && val(s[0])[0]); }
        if (eo === "2.5.29.17") {
          const g = kids(node(val(ev), 0));
          if (g.some((x) => (x.tag & 0x1f) === 5)) { san = ""; continue; }       // EDIPartyName: cryptography 미지원 → SAN 비움
          san = g.map((x) => { const tg = x.tag & 0x1f, v = val(x);
            if (tg === 2 || tg === 1 || tg === 6) return Array.from(v, (ch) => String.fromCharCode(ch)).join("");
            if (tg === 7) return v.length === 4 ? Array.from(v).join(".") : Array.from({ length: 8 }, (_, k) => ((v[2 * k] << 8) | v[2 * k + 1]).toString(16)).join(":").replace(/(^|:)0(:0)+(:|$)/, "::");
            return null; }).filter((s) => s !== null).join(", ");
        }
      } catch { /* 확장 필드 읽기 실패 시 비워 두고 필수 항목은 계속 */ }
    }
    const fp = new Uint8Array(await subtle.digest("SHA-256", raw(c)));
    return { file_name: fileName, subject_cn: subject.cn, subject_dn: subject.dn, issuer_cn: issuer.cn, issuer_dn: issuer.dn,
      not_before: time(nb), not_after: time(na), serial_number: serial, version, public_key: keyText(spki),
      signature_algorithm: SIG[oid(kids(sigAlg)[0])] || oid(kids(sigAlg)[0]), cert_type: ca ? "CA 인증서" : "서버 인증서", san,
      sha256_fingerprint: Array.from(fp, (x) => x.toString(16).padStart(2, "0").toUpperCase()).join(":") };
  }

  // ───────── PEM
  function b64(s) { const bin = atob(s.replace(/[^A-Za-z0-9+/=]/g, "")); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
  function pemBlocks(text) { const re = /-----BEGIN CERTIFICATE-----([\s\S]*?)-----END CERTIFICATE-----/g, out = []; let m; while ((m = re.exec(text))) out.push(b64(m[1])); return out; }

  // ───────── PKCS#12 (인증서 가방만)
  const PRF = { "1.2.840.113549.2.7": "SHA-1", "1.2.840.113549.2.9": "SHA-256", "1.2.840.113549.2.10": "SHA-384", "1.2.840.113549.2.11": "SHA-512" };
  function octets(n) { if (!n.cons) return val(n); const parts = kids(n).map(octets); const out = new Uint8Array(parts.reduce((a, b) => a + b.length, 0)); let p = 0; for (const x of parts) { out.set(x, p); p += x.length; } return out; }
  async function pbes2(algParams, data, password) {
    const [kdf, enc] = kids(algParams);
    const [kdfO, kdfP] = kids(kdf); if (oid(kdfO) !== "1.2.840.113549.1.5.12") throw new CertError("지원하지 않는 PFX 키 유도 방식");
    const kp = kids(kdfP), salt = val(kp[0]); let iter = 0; for (const x of val(kp[1])) iter = iter * 256 + x;
    const prfN = kp.find((x) => x.tag === 0x30); const hash = prfN ? PRF[oid(kids(prfN)[0])] : "SHA-1";
    const [encO, iv] = kids(enc); const eo = oid(encO), len = { "2.16.840.1.101.3.4.1.2": 16, "2.16.840.1.101.3.4.1.22": 24, "2.16.840.1.101.3.4.1.42": 32 }[eo];
    if (!len || !hash) throw new CertError("지원하지 않는 PFX 암호화 방식");
    const base = await subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await subtle.deriveKey({ name: "PBKDF2", salt, iterations: iter, hash }, base, { name: "AES-CBC", length: len * 8 }, false, ["decrypt"]);
    return new Uint8Array(await subtle.decrypt({ name: "AES-CBC", iv: val(iv) }, key, data));
  }
  async function pkcs12Certs(bytes, passwords) {
    let pfx; try { pfx = node(bytes, 0); kids(pfx); } catch { throw new CertError("PFX 형식 오류"); }
    const authSafe = kids(pfx)[1], content = octets(kids(kids(authSafe)[1])[0]);
    const infos = kids(node(content, 0));
    const tries = [null, ...passwords.filter(Boolean)];
    for (const pw of tries) {
      try {
        const certs = [];
        for (const ci of infos) {
          const [ct, body] = kids(ci), t = oid(ct);
          let safe;
          if (t === "1.2.840.113549.1.7.1") safe = octets(kids(body)[0]);
          else if (t === "1.2.840.113549.1.7.6") {
            const eci = kids(kids(body)[0])[1];
            const [, alg, encC] = kids(eci), [ao, ap] = kids(alg), a = oid(ao);
            if (a.startsWith("1.2.840.113549.1.12.1.")) throw new CertError("구형 PFX 암호화(3DES·RC2)는 브라우저에서 열 수 없음 — PC에서 실행하세요");
            if (a !== "1.2.840.113549.1.5.13") throw new CertError("지원하지 않는 PFX 암호화 방식");
            if (pw === null) throw new CertError("__pw");
            safe = await pbes2(ap, octets(encC), pw);
          } else continue;
          for (const bag of kids(node(safe, 0))) {
            const [bo, bv] = kids(bag);
            if (oid(bo) !== "1.2.840.113549.1.12.10.1.3") continue;          // certBag 만 (개인키 가방은 열지 않음)
            const cb = kids(kids(bv)[0]); certs.push(octets(kids(cb[1])[0]));
          }
        }
        return certs;
      } catch (e) {
        if (e instanceof CertError && e.message !== "__pw") throw e;
      }
    }
    throw new CertError("비밀번호 필요");
  }

  // ───────── load_certs
  async function loadCerts(name, bytes, passwords) {
    const ext = "." + name.split(".").pop().toLowerCase();
    if (ext === ".pfx" || ext === ".p12") return pkcs12Certs(bytes, passwords);
    const text = Array.from(bytes.subarray(0, Math.min(bytes.length, 1 << 20)), (c) => String.fromCharCode(c)).join("");
    if (text.includes("-----BEGIN CERTIFICATE")) { const blocks = pemBlocks(text); if (!blocks.length) throw new CertError("PEM 형식 오류"); return blocks; }
    if (text.includes("-----BEGIN")) throw new CertError("인증서가 아닌 PEM (개인키 등)");
    return [bytes];
  }
  async function parseFile(name, bytes, passwords) {
    const ders = await loadCerts(name, bytes, passwords || []);
    const out = [];
    for (const d of ders) {
      try { out.push(await toRecord(d, name)); }
      catch (e) { throw new CertError(e instanceof CertError ? `형식 오류: ${e.message}` : "형식 오류: 인증서로 해석할 수 없음"); }
    }
    return out;
  }
  const isTarget = (path) => EXTS.includes("." + path.split(".").pop().toLowerCase()) && path.includes(".");

  const api = { parseFile, isTarget, EXTS, CertError };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.CertScanner = api;
})(typeof window !== "undefined" ? window : globalThis);
