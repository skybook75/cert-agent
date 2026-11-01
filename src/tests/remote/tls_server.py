"""테스트용 TLS 서버: 접속마다 인증서·키 파일을 다시 읽어 응답 (nginx reload 대신)."""
import socket, ssl, sys, threading
cert, key, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", port)); srv.listen(20)
def handle(c):
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
        with ctx.wrap_socket(c, server_side=True) as t:
            t.recv(10)
    except Exception:
        pass
while True:
    c, _ = srv.accept(); threading.Thread(target=handle, args=(c,), daemon=True).start()
