#!/usr/bin/env python
"""Part 포털 로컬 서버: 웹 UI(docs/) + 원본 PDF(Part_Data/ → /pdfs/) 를 함께 제공.

    python serve.py                       # http://127.0.0.1:8080 (PDF 폴더: ./Part_Data)
    python serve.py --pdf-dir D:\\도면 --port 9000 --open

- 웹에서 Part 를 클릭 → "PDF 해당 페이지 열기" 가 /pdfs/<문서>#page=N 으로 열림
- 큰 PDF(1GB+)도 필요한 구간만 전송(HTTP Range)하므로 바로 열림
- 기본으로 내 PC(127.0.0.1)에서만 접속 가능 (기밀 도면이 네트워크에 노출되지 않도록).
  사내 다른 PC에서도 보려면 --host 0.0.0.0 (보안 정책 확인 후)

Part Indexer 앱은 이 모듈을 가져다 쓴다(make_server):
  index_path / notes_path 로 현재 인덱스·설명 파일을 지정하고, doc_paths 로 문서→원본 경로를 알려준다.
"""
import argparse
import json
import os
import re
import sys
import threading
import urllib.parse
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    pdf_dir = ROOT / "Part_Data"
    index_path = None     # 지정하면 /data/parts_index.json 대신 이 파일을 제공
    notes_path = None     # 지정하면 /data/notes.json 을 이 파일로 제공하고 /api/notes 로 저장 가능
    doc_paths = {}        # {문서 상대경로: 원본 파일 절대경로} — 인덱스에 있는 파일만 열람 허용
    api_handler = None    # 앱 모드: (method, path, body_dict) -> (code, obj)  — /api/app/* 요청 처리
    token = ""            # 앱 모드: /api/app/* 호출에 필요한 실행별 토큰 (다른 웹사이트의 위조 호출 차단)
    strict_host = False   # 앱 모드: Host 가 127.0.0.1/localhost 가 아니면 거부 (DNS rebinding 방어)

    def _guard(self):
        """앱 모드 보안 검사. 통과하면 True, 거부하면 응답을 보내고 False."""
        if self.strict_host and self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            self._json(403, {"ok": False})
            return False
        if self.api_handler and self.path.startswith("/api/app/"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if self.headers.get("X-Token") != self.token and q.get("t", [""])[0] != self.token:  # t= : sendBeacon 용
                self._json(403, {"ok": False, "error": "token"})
                return False
        return True

    def _api(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except ValueError:
            body = {}
        try:
            code, obj = self.api_handler(method, urllib.parse.urlsplit(self.path).path, body)
        except Exception as e:  # 컨트롤러 오류는 화면에 표시
            code, obj = 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._json(code, obj)

    def translate_path(self, path):
        p = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        if p in ("/app", "/app/"):
            return str(Path(self.directory) / "app.html")
        if p == "/data/parts_index.json" and self.index_path:
            return str(self.index_path)
        if p == "/data/notes.json" and self.notes_path:
            return str(self.notes_path)
        if p.startswith("/pdfs/"):
            rel = p[len("/pdfs/"):]
            hit = self.doc_paths.get(rel)
            if hit and os.path.isfile(hit):
                return str(hit)
            base = Path(self.pdf_dir).resolve()
            target = (base / rel).resolve()
            if base not in target.parents and target != base:
                return str(base / "__forbidden__")  # 폴더 밖 접근 차단
            return str(target)
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        if self.path.startswith(("/data/", "/api/")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._guard():
            return
        if self.api_handler and self.path.startswith("/api/app/"):
            return self._api("POST")
        if self.path == "/api/notes" and self.notes_path:
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                return self._json(403, {"ok": False})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 50 * 1024 * 1024:
                return self._json(413, {"ok": False})
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                Path(self.notes_path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                return self._json(400, {"ok": False})
            return self._json(200, {"ok": True})
        self._json(404, {"ok": False})

    def do_GET(self):
        if not self._guard():
            return
        if self.api_handler and self.path.startswith("/api/app/"):
            return self._api("GET")
        if self.path.startswith("/api/ping"):
            return self._json(200, {"app": bool(self.notes_path)})
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if not (m and os.path.isfile(path)):
            return super().do_GET()
        size = os.path.getsize(path)
        a, b = m.groups()
        if a:
            start, end = int(a), (int(b) if b else size - 1)
        else:  # bytes=-N (마지막 N바이트)
            start, end = max(size - int(b or 0), 0), size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 브라우저가 중간에 연결을 닫음(정상)

    def log_message(self, fmt, *args):
        if sys.stderr is None or "/pdfs/" in self.path:  # 창 모드 앱엔 stderr 없음 / PDF 구간 요청 로그는 생략
            return
        super().log_message(fmt, *args)


def make_server(web_dir, host="127.0.0.1", port=0):
    """port=0 이면 빈 포트를 자동 선택. 반환: ThreadingHTTPServer (server_address[1] 이 실제 포트)."""
    return ThreadingHTTPServer((host, port), partial(Handler, directory=str(web_dir)))


def start_background(srv):
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf-dir", default=str(ROOT / "Part_Data"), help="원본 PDF 폴더 (기본: ./Part_Data)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1(내 PC만). 0.0.0.0 은 같은 네트워크 전체에 공개")
    ap.add_argument("--open", action="store_true", help="브라우저 자동 실행")
    a = ap.parse_args()

    Handler.pdf_dir = Path(a.pdf_dir)
    if not Handler.pdf_dir.is_dir():
        sys.exit(f"PDF 폴더가 없습니다: {Handler.pdf_dir}")
    srv = make_server(ROOT / "docs", a.host, a.port)
    url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}/"
    print(f"Part 포털: {url}\nPDF 폴더 : {Handler.pdf_dir.resolve()}  →  /pdfs/\n(종료: Ctrl+C)")
    if a.host == "0.0.0.0":
        print("⚠ 같은 네트워크의 다른 PC에서도 접속·PDF 열람이 가능합니다. 보안 정책을 확인하세요.")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
