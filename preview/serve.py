"""웹 화면 로컬 미리보기 (가짜 데이터).

    python preview/serve.py      → http://localhost:8000

web/ 폴더를 그대로 띄우되, Firebase 모듈만 preview/mock-firebase.js로 바꿔 끼운다.
실제 Firebase에는 연결하지 않으며, 새로고침하면 데이터가 처음 상태로 돌아간다.
"""
import http.server
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
MOCK = os.path.join(ROOT, "preview", "mock-firebase.js")
FB = "https://www.gstatic.com/firebasejs/10.12.2/"

IMPORT_MAP = json.dumps({"imports": {
    FB + "firebase-app.js": "/__preview__/mock-firebase.js",
    FB + "firebase-auth.js": "/__preview__/mock-firebase.js",
    FB + "firebase-firestore.js": "/__preview__/mock-firebase.js",
}})
# UI 키트의 안내 띠(가이드 2-5): 헤더 바로 아래 화면 전체 폭
BANNER = '<div class="ui-banner">로컬 미리보기 : 가짜 데이터이며 새로 고치면 처음 상태로 돌아갑니다.</div>'


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def send_body(self, body, ctype):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                html = f.read()
            html = html.replace("<head>", f'<head>\n  <script type="importmap">{IMPORT_MAP}</script>', 1)
            html = html.replace("</header>", "</header>\n" + BANNER, 1)
            return self.send_body(html, "text/html; charset=utf-8")
        if path == "/__/firebase/init.json":
            return self.send_body("{}", "application/json")
        if path == "/__preview__/mock-firebase.js":
            with open(MOCK, encoding="utf-8") as f:
                return self.send_body(f.read(), "text/javascript; charset=utf-8")
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"미리보기: http://localhost:{port}  (종료: Ctrl+C)")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
