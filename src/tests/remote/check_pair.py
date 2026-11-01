"""nginx -t 대용: 인증서 체인 파일과 개인키가 짝이 맞는지 검사."""
import ssl, sys
try:
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(sys.argv[1], sys.argv[2]); print("syntax is ok / test is successful")
except Exception as e:
    print("SSL_CTX_use_PrivateKey failed:", e); sys.exit(1)
