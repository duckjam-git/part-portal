#!/usr/bin/env python
"""Part Indexer — 도면·매뉴얼·이미지·txt/csv/xlsx 를 인덱싱하고 검색까지 하는 단독 실행 앱.

    python part_indexer_app.py          # 개발 중 실행
    PartIndexer.exe                     # build_exe.bat 으로 만든 단독 실행본 (Python 설치 불필요)

구조: 보이지 않는 Python 프로세스(내장 서버 + 파일 선택 창) + 웹 화면(docs/app.html, Edge/Chrome 앱 창).
  · 화면을 닫으면 앱도 종료 (인덱싱 중이면 끝난 뒤 종료), 이미 실행 중이면 같은 창을 다시 연다.
  · 인덱싱은 파일마다 별도 프로세스(--worker)로 실행되어, 특정 페이지에서 라이브러리가 비정상 종료돼도 앱은 유지된다.
"""
import os
import sys

# 창 모드(exe)에서는 stdout/stderr 가 없다 → 출력 코드가 죽지 않도록 대체
for _n in ("stdout", "stderr"):
    if getattr(sys, _n, None) is None:
        setattr(sys, _n, open(os.devnull, "w"))

from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = Path(getattr(sys, "_MEIPASS", HERE))          # exe 로 묶였을 때 리소스 위치
for _p in (HERE / "indexer", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import appcore  # noqa: E402

if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--worker":
    sys.exit(appcore.worker_main(sys.argv[2]))

import json  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402

import index_pdfs as ix  # noqa: E402
import serve  # noqa: E402

APP_NAME = "Part Indexer"
APPDIR = Path(os.environ.get("APPDATA", Path.home())) / "PartIndexer"
SETTINGS, RUNFILE = APPDIR / "settings.json", APPDIR / "run.json"
FILETYPES = [("지원 파일", " ".join("*" + e for e in sorted(ix.EXTS))),
             ("PDF", "*.pdf"), ("이미지", " ".join("*" + e for e in sorted(ix.IMAGE_EXTS))),
             ("txt / csv / xlsx", " ".join("*" + e for e in sorted(ix.TEXT_EXTS | ix.TABLE_EXTS))),
             ("모든 파일", "*.*")]


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Controller:
    """화면(app.html)이 호출하는 모든 동작. 화면 없이도 테스트 가능하도록 UI 의존성은 ui(fn) 하나뿐."""

    def __init__(self, ui):
        self.ui = ui                       # ui(fn): 메인(Tk) 스레드에서 fn(root) 실행 후 결과 반환 — 파일 선택 창용
        self.lock = threading.RLock()
        self.idx, self.idx_path = appcore.empty_index(), None
        self.files, self.nid = [], 0
        self.running = self.cancel = False
        self.proc = None
        self.log, self.notice = [], "파일을 추가하고 Indexing 을 누르세요."
        self.prog = {}
        self.t0 = self.elapsed = 0.0
        self.run_seq = 0
        self.last_run = {}
        self.opts = {}
        self.last_hb, self.bye_at, self.quit = time.time(), None, False
        self.viewer_port = None
        try:
            self.cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            self.cfg = {}

    # ───────────── 라우팅 ─────────────
    def handle(self, method, path, body):
        name = path.rsplit("/", 1)[-1]
        if name != "bye":
            self.last_hb, self.bye_at = time.time(), None
        fn = getattr(self, "api_" + name, None)
        if not fn:
            return 404, {"ok": False, "error": "unknown"}
        with self.lock if name in ("state",) else _NoLock():
            r = fn(body)
        return 200, r

    def _msg(self, s):
        self.notice = s

    def _log(self, s):
        self.log.append(s)
        del self.log[:-1000]

    # ───────────── 상태 ─────────────
    def api_state(self, _=None):
        with self.lock:
            idx = self.idx
            return {
                "ok": True, "cfg": self.cfg, "running": self.running, "notice": self.notice, "prog": self.prog,
                "elapsed": (time.time() - self.t0) if self.running else self.elapsed, "run_seq": self.run_seq,
                "last_run": self.last_run, "log": self.log[-400:], "log_n": len(self.log),
                "files": [{"id": f["id"], "name": f["path"].name, "folder": str(f["path"].parent), "size": f["size"],
                           "state": f["state"]} for f in self.files],
                "index": {"path": str(self.idx_path) if self.idx_path else "", "parts": len(idx["parts"]), "pages": len(idx["pages"]),
                          "docs": [{"file": d["file"], "type": d["type"], "kind": d.get("kind", ""), "pages": d.get("pages", 0),
                                    "count": d.get("count", 0), "ocr": d.get("ocr_pages", 0), "warnings": d.get("warnings", [])}
                                   for d in idx["docs"]],
                          "manual": appcore.manual_parts(idx)},
            }

    def api_hb(self, _=None):
        return {"ok": True}

    def api_bye(self, _=None):
        self.bye_at = time.time()
        return {"ok": True}

    def api_quit(self, _=None):
        self.quit = True
        return {"ok": True}

    # ───────────── 파일 목록 ─────────────
    def add_paths(self, paths, root=None):
        have = {f["path"] for f in self.files}
        n = 0
        for p in paths:
            p = Path(p).resolve()
            if p in have or p.suffix.lower() not in ix.EXTS or not p.is_file():
                continue
            try:
                size = human(p.stat().st_size)
            except OSError:
                size = "?"
            self.nid += 1
            self.files.append({"id": self.nid, "path": p, "root": root.resolve() if root else p.parent, "size": size, "state": "대기"})
            have.add(p)
            n += 1
        return n

    def api_pick_files(self, _=None):
        ps = self.ui(lambda r: __import__("tkinter.filedialog", fromlist=["x"]).askopenfilenames(
            parent=r, title="인덱싱할 파일 선택", filetypes=FILETYPES, initialdir=self.cfg.get("dir") or None))
        if ps:
            with self.lock:
                self.cfg["dir"] = str(Path(ps[0]).parent)
                n = self.add_paths([Path(p) for p in ps])
                self._msg(f"{n}개 파일 추가")
        return {"ok": True}

    def api_pick_folder(self, _=None):
        d = self.ui(lambda r: __import__("tkinter.filedialog", fromlist=["x"]).askdirectory(
            parent=r, title="폴더 선택 (하위 폴더 포함)", initialdir=self.cfg.get("dir") or None))
        if d:
            root = Path(d)
            with self.lock:
                self.cfg["dir"] = d
                n = self.add_paths(sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ix.EXTS), root=root)
                self._msg(f"폴더에서 {n}개 파일 추가")
        return {"ok": True}

    def api_add(self, b):
        """경로를 직접 지정해 추가 (파일 또는 폴더). 자동 테스트/향후 확장용 — 토큰이 있어야 호출 가능."""
        n = 0
        with self.lock:
            for s in b.get("paths", []):
                p = Path(s)
                if p.is_dir():
                    n += self.add_paths(sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in ix.EXTS), root=p)
                else:
                    n += self.add_paths([p])
            self._msg(f"{n}개 파일 추가")
        return {"ok": True, "added": n}

    def api_remove(self, b):
        with self.lock:
            if not self.running:
                ids = set(b.get("ids", []))
                self.files = [f for f in self.files if f["id"] not in ids]
        return {"ok": True}

    def api_clear(self, _=None):
        with self.lock:
            if not self.running:
                self.files = []
        return {"ok": True}

    # ───────────── 인덱스 파일 ─────────────
    def api_pick_out(self, b):
        cur = Path(b.get("out") or "")
        p = self.ui(lambda r: __import__("tkinter.filedialog", fromlist=["x"]).asksaveasfilename(
            parent=r, title="출력 파일 이름 지정", defaultextension=".json", filetypes=[("인덱스 JSON", "*.json")],
            initialfile=cur.name or "parts_index.json",
            initialdir=str(cur.parent) if b.get("out") else (self.cfg.get("dir") or None)))
        return {"ok": True, "path": str(Path(p)) if p else ""}

    def api_open_index(self, _=None):
        p = self.ui(lambda r: __import__("tkinter.filedialog", fromlist=["x"]).askopenfilename(
            parent=r, title="기존 인덱스(JSON) 열기", filetypes=[("인덱스 JSON", "*.json")], initialdir=self.cfg.get("dir") or None))
        if not p:
            return {"ok": True, "path": ""}
        with self.lock:
            if self.running:
                return {"ok": False, "error": "인덱싱 중에는 열 수 없습니다."}
            try:
                self.idx = appcore.load_index(p)
            except Exception as e:
                return {"ok": False, "error": f"인덱스를 읽지 못했습니다: {e}"}
            self.idx_path = Path(p)
            self.cfg["dir"] = str(Path(p).parent)
            self._msg(f"인덱스 열기: 문서 {len(self.idx['docs'])}개 / Part {len(self.idx['parts']):,}건")
        return {"ok": True, "path": p}

    def api_new_index(self, _=None):
        with self.lock:
            if self.running:
                return {"ok": False, "error": "인덱싱 중에는 비울 수 없습니다."}
            self.idx, self.idx_path = appcore.empty_index(), None
            self._msg("새 인덱스")
        return {"ok": True}

    def _save(self, out, want_json=True, want_xlsx=True):
        """메모리 인덱스를 저장. (ok, 메시지). JSON 은 검색 화면의 기준 파일이라 항상 기록."""
        out = Path(out)
        out = out if out.suffix.lower() == ".json" else out.with_suffix(".json")
        appcore.finalize(self.idx)
        done = []
        try:
            appcore.write_json(self.idx, out)
            done.append(str(out))
            self.idx_path = out
            if out.parent.name.lower() == "data":       # docs/data 에 저장하면 웹 화면용 목록도 갱신
                try:
                    appcore.update_manifest(out.parent)
                    done.append("manifest.json")
                except Exception:
                    pass
            if want_xlsx:
                notes = {}
                try:
                    notes = json.loads(out.with_name(out.stem + ".notes.json").read_text(encoding="utf-8"))
                except Exception:
                    pass
                x = out.with_suffix(".xlsx")
                appcore.write_xlsx(self.idx, x, notes)
                done.append(str(x))
        except PermissionError as e:
            return False, f"저장 실패 — 파일이 다른 프로그램(Excel 등)에서 열려 있는지 확인하세요. ({e})"
        except Exception as e:
            return False, f"저장 실패: {e}"
        return True, "저장 완료: " + " , ".join(done)

    def _remember(self, b):
        for k in ("ocr", "type", "append", "json", "xlsx", "out"):
            if k in b:
                self.cfg[k] = b[k]
        try:
            APPDIR.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def api_save(self, b):
        with self.lock:
            if self.running:
                return {"ok": False, "error": "인덱싱 중에는 저장할 수 없습니다."}
            if not (b.get("out") or "").strip():
                return {"ok": False, "error": "출력 파일 이름을 먼저 지정하세요."}
            ok, msg = self._save(b["out"], b.get("json", True), b.get("xlsx", True))
            self._remember(b)
            self._msg(msg)
        return {"ok": ok, "error": "" if ok else msg, "msg": msg}

    # ───────────── 수동 입력 ─────────────
    def api_manual(self, b):
        op, f = b.get("op"), b.get("fields", {})
        with self.lock:
            if op in ("add", "update"):
                if not (str(f.get("name", "")).strip() or str(f.get("pn", "")).strip()):
                    return {"ok": False, "error": "Part 이름 또는 품번은 입력해야 합니다."}
                if str(f.get("page", "")).strip() and not str(f["page"]).strip().isdigit():
                    return {"ok": False, "error": "페이지는 숫자로 입력하세요."}
            if op == "add":
                self.idx["parts"].append(appcore.new_manual(f))
                self._msg("수동 항목 추가 — [지금 저장] 또는 Indexing 시 파일에 기록됩니다.")
            elif op == "update":
                for i, p in enumerate(self.idx["parts"]):
                    if p["id"] == b.get("id") and p.get("src") == "manual":
                        self.idx["parts"][i] = appcore.new_manual(f, pid=p["id"])   # id 유지 → 설명(메모) 유지
            elif op == "delete":
                self.idx["parts"] = [p for p in self.idx["parts"] if not (p["id"] == b.get("id") and p.get("src") == "manual")]
        return {"ok": True}

    # ───────────── 인덱싱 실행 ─────────────
    def api_start(self, b):
        with self.lock:
            if self.running:
                return {"ok": False, "error": "이미 실행 중입니다."}
            if not self.files:
                return {"ok": False, "error": "먼저 [파일 추가] 로 인덱싱할 파일을 선택하세요."}
            out = (b.get("out") or "").strip()
            if not out:
                return {"ok": False, "error": "출력 파일 이름을 지정하세요."}
            out = Path(out)
            out = out if out.suffix.lower() == ".json" else out.with_suffix(".json")
            pages = (b.get("pages") or "").strip()
            if pages and not re.fullmatch(r"\d+(-\d+)?", pages):
                return {"ok": False, "error": "페이지 범위는 1-20 또는 5 형식으로 입력하세요."}
            append = bool(b.get("append", True))
            manual = appcore.manual_parts(self.idx)
            if append:
                if out.exists() and self.idx_path != out:      # 다른 파일이면 불러옴 (현재 수동 항목은 유지)
                    try:
                        loaded = appcore.load_index(out)
                        ids = {p["id"] for p in loaded["parts"]}
                        loaded["parts"] += [m for m in manual if m["id"] not in ids]
                        self.idx = loaded
                    except Exception as e:
                        return {"ok": False, "error": f"기존 출력 파일을 읽지 못했습니다({e}). '이어서 추가'를 끄고 새로 만들 수 있습니다."}
            else:
                self.idx = appcore.empty_index()
                self.idx["parts"] += manual
            by_file = {d["file"]: d for d in self.idx["docs"]}
            jobs, seen = [], {}
            for f in self.files:
                rel = f["path"].relative_to(f["root"]).as_posix()
                rel = appcore.unique_rel(self.idx, rel, str(f["path"]))
                while rel in seen and seen[rel] != f["path"]:
                    rel = f["path"].as_posix().replace(":", "")
                seen[rel] = f["path"]
                jobs.append({"f": f, "rel": rel, "prev_hash": (by_file.get(rel) or {}).get("hash", "") if append else ""})
                f["state"] = "대기"
            self.opts = {"mode": b.get("ocr", "auto"), "force_type": b.get("type", ""), "pages": pages}
            self._remember(b)
            self.running, self.cancel = True, False
            self.t0, self.elapsed = time.time(), 0.0
            self.log = []
            self.prog = {"i": 0, "total": len(jobs), "name": "", "msg": "시작", "all": 0, "file": 0}
            self._msg(f"인덱싱 시작: {len(jobs)}개 파일")
            threading.Thread(target=self._work, args=(jobs, dict(self.opts), out, bool(b.get("json", True)), bool(b.get("xlsx", True))),
                             daemon=True).start()
        return {"ok": True}

    def api_stop(self, _=None):
        with self.lock:
            if self.running:
                self.cancel = True
                self._msg("취소 중… (현재 작업 프로세스를 종료합니다)")
                p = self.proc
                if p and p.poll() is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
        return {"ok": True}

    @staticmethod
    def _cmd(jobfile):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--worker", jobfile]
        return [sys.executable, str(Path(__file__).resolve()), "--worker", jobfile]

    def _work(self, jobs, opts, out, want_json, want_xlsx):
        """백그라운드 스레드: 파일마다 worker 프로세스 실행 → 진행/결과를 상태에 반영."""
        tmp = Path(tempfile.mkdtemp(prefix="partidx_"))
        total = len(jobs)
        try:
            for i, j in enumerate(jobs):
                if self.cancel:
                    break
                f = j["f"]
                with self.lock:
                    f["state"] = "진행 중…"
                    self.prog.update(i=i + 1, name=f["path"].name, msg="준비", file=0)
                    self._log(f"[{i + 1}/{total}] {j['rel']}")
                job = {"path": str(f["path"]), "root": str(f["root"]), "rel": j["rel"], "prev_hash": j["prev_hash"],
                       "mode": opts["mode"], "pages": opts["pages"], "force_type": opts["force_type"], "dpi": 0,
                       "events": str(tmp / f"ev{i}.jsonl"), "result": str(tmp / f"res{i}.json")}
                jf = tmp / f"job{i}.json"
                jf.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                Path(job["events"]).write_text("", encoding="utf-8")
                flags = 0x08000000 if os.name == "nt" else 0           # CREATE_NO_WINDOW
                self.proc = subprocess.Popen(self._cmd(str(jf)), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, creationflags=flags)
                pos, cached, err = 0, False, ""
                while True:
                    alive = self.proc.poll() is None
                    with open(job["events"], "rb") as fh:
                        fh.seek(pos)
                        data = fh.read()
                    nl = data.rfind(b"\n")
                    if nl >= 0:
                        pos += nl + 1
                        for line in data[:nl + 1].decode("utf-8", "replace").splitlines():
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            if e["ev"] == "page":
                                with self.lock:
                                    self.prog.update(msg=e["msg"], file=100 * e["done"] / max(e["total"], 1))
                            elif e["ev"] == "cached":
                                cached = True
                            elif e["ev"] == "error":
                                err = e["msg"]
                    if not alive:
                        break
                    time.sleep(0.15)
                rc = self.proc.returncode
                with self.lock:
                    if self.cancel:
                        f["state"] = "취소됨"
                        self._log("취소되었습니다.")
                        break
                    if cached:
                        doc = next((d for d in self.idx["docs"] if d["file"] == j["rel"]), None)
                        if doc is not None:
                            doc["abs"] = str(f["path"])
                            if opts["force_type"]:
                                doc["type"] = opts["force_type"]
                        f["state"] = "변경 없음 (건너뜀)"
                        self._log("    → 변경 없음, 이전 결과 사용")
                    elif Path(job["result"]).exists():
                        try:
                            res = json.loads(Path(job["result"]).read_text(encoding="utf-8"))
                            appcore.merge_result(self.idx, res, str(f["path"]))
                            d = res["doc"]
                            f["state"] = f"완료 · {d['count']:,}건" + (" ⚠" if d["warnings"] else "")
                            self._log(f"    → {d['count']:,}건 / {d['pages']}p" + (f"  ⚠ {'; '.join(d['warnings'])}" if d["warnings"] else ""))
                        except Exception as ex:
                            self._fail(f, j["rel"], f"결과 읽기 실패: {ex}")
                    else:
                        last = err.strip().splitlines()[-1] if err.strip() else f"프로세스가 비정상 종료됨 (코드 {rc})"
                        self._fail(f, j["rel"], last)
                    self.prog.update(all=100 * (i + 1) / total, file=100)
        finally:
            with self.lock:
                self.proc = None
                self.elapsed = time.time() - self.t0
                was_cancel = self.cancel
                ok, msg = self._save(out, want_json, want_xlsx)
                self._log(("취소" if was_cancel else "완료") + f" — 소요 {int(self.elapsed)}초 / 문서 {len(self.idx['docs'])}개 / "
                          f"Part {len(self.idx['parts']):,}건" + ("" if ok else " (저장 안 됨)"))
                self._msg(msg)
                self.last_run = {"cancelled": was_cancel, "ok": ok, "msg": msg}
                self.run_seq += 1
                self.running = False
                self.prog.update(msg="취소됨" if was_cancel else "완료", name="")
            shutil.rmtree(tmp, ignore_errors=True)

    def _fail(self, f, rel, msg):
        appcore.drop_doc(self.idx, rel)
        self.idx["docs"].append({"file": rel, "type": "drawing", "kind": ix.doc_kind(f["path"]), "pages": 0, "hash": "",
                                 "count": 0, "ocr_pages": 0, "warnings": [f"인덱싱 실패: {msg}"], "abs": str(f["path"])})
        f["state"] = "실패"
        self._log(f"    ✖ 실패: {msg}")

    # ───────────── 검색 화면 ─────────────
    def api_open_viewer(self, b):
        with self.lock:
            if self.running:
                return {"ok": False, "error": "인덱싱이 끝난 뒤에 열 수 있습니다."}
            if not self.idx["parts"] and not self.idx["docs"]:
                return {"ok": False, "error": "검색할 인덱스가 없습니다. 인덱싱하거나 [기존 인덱스 열기] 로 불러오세요."}
            if not (b.get("out") or "").strip():
                return {"ok": False, "error": "출력 파일 이름을 먼저 지정하세요. (검색 화면은 저장된 JSON 을 읽습니다)"}
            ok, msg = self._save(b["out"], b.get("json", True), b.get("xlsx", True))   # 화면은 저장된 JSON 을 읽는다 (항상 최신)
            self._remember(b)
            if not ok:
                return {"ok": False, "error": msg}
            out = self.idx_path
            serve.Handler.index_path = out
            serve.Handler.notes_path = out.with_name(out.stem + ".notes.json")
            serve.Handler.doc_paths = {d["file"]: d["abs"] for d in self.idx["docs"] if d.get("abs")}
            self._msg(msg)
        webbrowser.open(f"http://127.0.0.1:{self.viewer_port}/")
        return {"ok": True}


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ───────────────────────── 실행 ─────────────────────────
def find_app_browser():
    """--app 모드를 지원하는 Edge/Chrome 경로. 없으면 None (기본 브라우저로 대체)."""
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        b = os.environ.get(env)
        if b:
            cands += [Path(b) / "Microsoft/Edge/Application/msedge.exe", Path(b) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url):
    exe = find_app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1320,920"])
            return
        except Exception:
            pass
    webbrowser.open(url)


def existing_instance():
    """이미 실행 중인 앱의 주소(토큰 포함)를 반환, 없으면 None."""
    try:
        info = json.loads(RUNFILE.read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/app/hb", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{info['port']}/app/#t={info['token']}"
    except Exception:
        pass
    return None


def main():
    url = existing_instance()
    if url:                       # 이미 실행 중 → 창만 다시 열고 종료
        open_window(url)
        return

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    q = queue.Queue()

    def ui(fn):                   # 다른 스레드에서 호출 → Tk 메인 스레드에서 실행(파일 선택 창)
        ev, box = threading.Event(), {}
        q.put((fn, ev, box))
        ev.wait(600)
        return box.get("r")

    ctl = Controller(ui)
    token = secrets.token_urlsafe(16)
    serve.Handler.api_handler = ctl.handle
    serve.Handler.token = token
    serve.Handler.strict_host = True
    srv = None
    for port in (0,):   # 고정 포트는 Windows 에서 다른 앱과 겹쳐 바인딩될 수 있어 항상 빈 포트를 받음
        try:
            srv = serve.make_server(BASE / "docs", "127.0.0.1", port)
            break
        except OSError:
            continue
    if srv is None:
        return
    ctl.viewer_port = srv.server_address[1]
    serve.start_background(srv)
    try:
        APPDIR.mkdir(parents=True, exist_ok=True)
        RUNFILE.write_text(json.dumps({"port": ctl.viewer_port, "token": token}), encoding="utf-8")
    except Exception:
        pass
    open_window(f"http://127.0.0.1:{ctl.viewer_port}/app/#t={token}")
    t_start = time.time()

    def pump():
        while True:
            try:
                fn, ev, box = q.get_nowait()
            except queue.Empty:
                break
            try:
                root.attributes("-topmost", True)
                box["r"] = fn(root)
            except Exception as e:
                box["r"] = None
                ctl.notice = f"창 열기 오류: {e}"
            finally:
                root.attributes("-topmost", False)
                ev.set()
        now = time.time()
        idle = (ctl.bye_at and now - ctl.bye_at > 4) or (now - ctl.last_hb > 900) or (now - t_start > 90 and now - ctl.last_hb > 90)
        if ctl.quit or (idle and not ctl.running):
            root.quit()
            return
        root.after(60, pump)

    root.after(60, pump)
    try:
        root.mainloop()
    finally:
        ctl.cancel = True
        p = ctl.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        try:
            srv.shutdown()
            RUNFILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()

