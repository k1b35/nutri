"""
Локальный запуск функции для проверки до деплоя.

  export YANDEX_API_KEY=...  FOLDER_ID=...
  python3 backend/dev_server.py            # слушает http://127.0.0.1:8080

Потом в index.html временно поставьте AI_ENDPOINT: "http://127.0.0.1:8080" и откройте сайт.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import index


class H(BaseHTTPRequestHandler):
    def _run(self):
        length = int(self.headers.get("Content-Length") or 0)
        event = {
            "httpMethod": self.command,
            "headers": {k: v for k, v in self.headers.items()},
            "body": self.rfile.read(length).decode("utf-8") if length else "",
            "isBase64Encoded": False,
        }
        res = index.handler(event, None)
        body = (res.get("body") or "").encode("utf-8")
        self.send_response(res.get("statusCode", 200))
        for k, v in (res.get("headers") or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_OPTIONS = _run

    def log_message(self, fmt, *args):
        sys.stderr.write("[dev] " + fmt % args + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Нутри dev-сервер: http://127.0.0.1:{port}  (модель: {index.MODEL})")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
