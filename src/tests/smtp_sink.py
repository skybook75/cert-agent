"""테스트용 SMTP 수신 서버 (발송 검증 전용). 받은 메일을 JSON 줄로 기록한다.

  python tests/smtp_sink.py <port> <out.jsonl>
"""
import email
import email.policy
import json
import socketserver
import sys

OUT = sys.argv[2] if len(sys.argv) > 2 else "mail.jsonl"


class H(socketserver.StreamRequestHandler):
    def w(self, s):
        self.wfile.write((s + "\r\n").encode())

    def handle(self):
        self.w("220 sink ESMTP")
        mail_from, rcpts = None, []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode(errors="replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                self.w("250-sink")
                self.w("250 8BITMIME")
            elif up.startswith("MAIL FROM"):
                mail_from, rcpts = cmd[10:].strip(" <>"), []
                self.w("250 OK")
            elif up.startswith("RCPT TO"):
                rcpts.append(cmd[8:].strip(" <>"))
                self.w("250 OK")
            elif up == "DATA":
                self.w("354 end with .")
                data = b""
                while True:
                    ln = self.rfile.readline()
                    if ln in (b".\r\n", b".\n", b""):
                        break
                    data += ln[1:] if ln.startswith(b"..") else ln
                msg = email.message_from_bytes(data, policy=email.policy.default)
                body = msg.get_body(preferencelist=("plain",))
                with open(OUT, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"from": mail_from, "to": rcpts, "subject": str(msg["Subject"]),
                                        "body": body.get_content() if body else ""}, ensure_ascii=False) + "\n")
                self.w("250 queued")
            elif up == "QUIT":
                self.w("221 bye")
                return
            elif up in ("RSET", "NOOP"):
                self.w("250 OK")
            else:
                self.w("502 not implemented")


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", int(sys.argv[1])), H) as s:
        s.serve_forever()
