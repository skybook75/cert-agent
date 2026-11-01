/* xlsx-lite — 외부 라이브러리 없는 최소 엑셀(.xlsx) 읽기·쓰기 (브라우저·Node 18+ 공용)
 * 읽기: ZIP 중앙 디렉터리 → 첫 번째 시트 + 공유 문자열 → 행 배열(셀 값: 문자열·숫자·불리언)
 * 쓰기: 시트 여러 개(첫 행 머리글 굵게) → 무압축 ZIP(.xlsx) 바이트
 * 압축 해제는 표준 DecompressionStream("deflate-raw") 사용.
 */
(function (root) {
  "use strict";
  const td = new TextDecoder("utf-8"), te = new TextEncoder();

  // ───────── ZIP 읽기
  async function inflateRaw(bytes) {
    const ds = new DecompressionStream("deflate-raw");
    const out = new Blob([bytes]).stream().pipeThrough(ds);
    return new Uint8Array(await new Response(out).arrayBuffer());
  }
  async function unzip(buf) {
    const u8 = new Uint8Array(buf), dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    let eocd = -1;
    for (let i = u8.length - 22; i >= Math.max(0, u8.length - 65557); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    if (eocd < 0) throw new Error("엑셀(.xlsx) 파일이 아닙니다");
    const n = dv.getUint16(eocd + 10, true); let p = dv.getUint32(eocd + 16, true);
    const files = {};
    for (let k = 0; k < n; k++) {
      if (dv.getUint32(p, true) !== 0x02014b50) throw new Error("손상된 엑셀 파일입니다");
      const method = dv.getUint16(p + 10, true), csize = dv.getUint32(p + 20, true);
      const nlen = dv.getUint16(p + 28, true), elen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
      const off = dv.getUint32(p + 42, true), name = td.decode(u8.subarray(p + 46, p + 46 + nlen));
      const lnlen = dv.getUint16(off + 26, true), lelen = dv.getUint16(off + 28, true);
      const data = u8.subarray(off + 30 + lnlen + lelen, off + 30 + lnlen + lelen + csize);
      files[name] = { method, data };
      p += 46 + nlen + elen + clen;
    }
    return {
      names: Object.keys(files),
      async text(name) {
        const f = files[name]; if (!f) return null;
        if (f.method === 0) return td.decode(f.data);
        if (f.method === 8) return td.decode(await inflateRaw(f.data));
        throw new Error("지원하지 않는 압축 방식");
      },
    };
  }

  // ───────── XML 도우미 (엑셀이 만드는 단순 구조만 처리)
  const unesc = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d)).replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16))).replace(/&amp;/g, "&");
  const textOf = (xml) => unesc((xml.match(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g) || []).map((t) => t.replace(/<t(?:\s[^>]*)?>|<\/t>/g, "")).join(""));
  function colIndex(ref) { let n = 0; for (const ch of ref.replace(/\d+$/, "")) n = n * 26 + (ch.charCodeAt(0) - 64); return n - 1; }

  async function read(buf) {
    const z = await unzip(buf);
    const shared = [];
    const ss = await z.text("xl/sharedStrings.xml");
    if (ss) for (const si of ss.match(/<si>[\s\S]*?<\/si>/g) || []) shared.push(textOf(si));
    let sheetPath = "xl/worksheets/sheet1.xml";
    const wb = await z.text("xl/workbook.xml"), rels = await z.text("xl/_rels/workbook.xml.rels");
    const first = wb && wb.match(/<sheet\b[^>]*r:id="([^"]+)"/);
    if (first && rels) {
      const rel = rels.match(new RegExp(`<Relationship\\b[^>]*Id="${first[1]}"[^>]*>`));
      const tgt = rel && rel[0].match(/Target="([^"]+)"/);
      if (tgt) sheetPath = tgt[1].startsWith("/") ? tgt[1].slice(1) : "xl/" + tgt[1].replace(/^\.\//, "");
    }
    const sheet = await z.text(sheetPath) || await z.text(z.names.find((n) => /^xl\/worksheets\/[^/]+\.xml$/.test(n)));
    if (!sheet) throw new Error("시트를 찾을 수 없습니다");
    const rows = [];
    for (const rm of sheet.match(/<row\b[^>]*>[\s\S]*?<\/row>|<row\b[^>]*\/>/g) || []) {
      const rnum = +(rm.match(/\br="(\d+)"/) || [0, rows.length + 1])[1];
      const row = [];
      for (const cm of rm.match(/<c\b[^>]*\/>|<c\b[^>]*>[\s\S]*?<\/c>/g) || []) {
        const ref = (cm.match(/\br="([A-Z]+\d+)"/) || [])[1];
        const t = (cm.match(/\bt="(\w+)"/) || [])[1];
        const v = (cm.match(/<v>([\s\S]*?)<\/v>/) || [])[1];
        let val = null;
        if (t === "s") val = shared[+v] ?? "";
        else if (t === "inlineStr") val = textOf(cm);
        else if (t === "str" || t === "e") val = v != null ? unesc(v) : "";
        else if (t === "b") val = v === "1";
        else if (v != null) val = Number(v);
        row[ref ? colIndex(ref) : row.length] = val;
      }
      rows[rnum - 1] = row;
    }
    return Array.from(rows, (r) => Array.from(r || [], (v) => (v === undefined ? null : v)));
  }

  // 엑셀 날짜 일련번호 → YYYY-MM-DD (1900 날짜 체계)
  function serialToDate(n) {
    const ms = Math.round((n - 25569) * 86400000);
    return new Date(ms).toISOString().slice(0, 10);
  }

  // ───────── 쓰기 (무압축 ZIP)
  const CRC = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
  function crc32(u8) { let c = 0xffffffff; for (let i = 0; i < u8.length; i++) c = CRC[(c ^ u8[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
  function zip(entries) {
    const parts = [], central = []; let off = 0;
    for (const [name, str] of entries) {
      const data = te.encode(str), nm = te.encode(name), crc = crc32(data);
      const h = new DataView(new ArrayBuffer(30));
      h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true); h.setUint16(8, 0, true);
      h.setUint32(14, crc, true); h.setUint32(18, data.length, true); h.setUint32(22, data.length, true); h.setUint16(26, nm.length, true);
      parts.push(new Uint8Array(h.buffer), nm, data);
      const c = new DataView(new ArrayBuffer(46));
      c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
      c.setUint32(16, crc, true); c.setUint32(20, data.length, true); c.setUint32(24, data.length, true); c.setUint16(28, nm.length, true);
      c.setUint32(42, off, true);
      central.push(new Uint8Array(c.buffer), nm);
      off += 30 + nm.length + data.length;
    }
    const csize = central.reduce((a, b) => a + b.length, 0);
    const e = new DataView(new ArrayBuffer(22));
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, entries.length, true); e.setUint16(10, entries.length, true);
    e.setUint32(12, csize, true); e.setUint32(16, off, true);
    const all = [...parts, ...central, new Uint8Array(e.buffer)];
    const out = new Uint8Array(all.reduce((a, b) => a + b.length, 0)); let p = 0;
    for (const a of all) { out.set(a, p); p += a.length; }
    return out;
  }
  const xesc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
  const colName = (i) => { let s = ""; i++; while (i) { const m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); } return s; };
  // sheets: [{ name, rows: [[...헤더], [...], ...], widths?: [..] }]
  function write(sheets) {
    const sheetXml = (rows, widths) => {
      const cols = widths ? `<cols>${widths.map((w, i) => `<col min="${i + 1}" max="${i + 1}" width="${w}" customWidth="1"/>`).join("")}</cols>` : "";
      const body = rows.map((r, ri) => `<row r="${ri + 1}">${r.map((v, ci) => {
        const ref = colName(ci) + (ri + 1), st = ri === 0 ? ' s="1"' : "";
        if (v === null || v === undefined || v === "") return "";
        if (typeof v === "number" && isFinite(v)) return `<c r="${ref}"${st}><v>${v}</v></c>`;
        return `<c r="${ref}" t="inlineStr"${st}><is><t xml:space="preserve">${xesc(v)}</t></is></c>`;
      }).join("")}</row>`).join("");
      return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>${cols}<sheetData>${body}</sheetData></worksheet>`;
    };
    const entries = [
      ["[Content_Types].xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>${sheets.map((_, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join("")}</Types>`],
      ["_rels/.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
      ["xl/workbook.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>${sheets.map((s, i) => `<sheet name="${xesc(String(s.name).slice(0, 31))}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")}</sheets></workbook>`],
      ["xl/_rels/workbook.xml.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${sheets.map((_, i) => `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join("")}<Relationship Id="rId${sheets.length + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`],
      ["xl/styles.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><sz val="11"/><name val="맑은 고딕"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs></styleSheet>`],
      ...sheets.map((s, i) => [`xl/worksheets/sheet${i + 1}.xml`, sheetXml(s.rows, s.widths)]),
    ];
    return zip(entries);
  }

  const api = { read, write, serialToDate, crc32, unzip };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.XlsxLite = api;
})(typeof window !== "undefined" ? window : globalThis);
