"""앱 공용 로직: 인덱스 읽기/쓰기/병합, 수동 항목, Excel 내보내기, 별도 프로세스 작업자(worker).

GUI(part_indexer_app.py)는 파일마다 worker 프로세스를 띄워 인덱싱한다.
→ PyMuPDF 가 특정 페이지에서 네이티브 크래시를 내도 GUI 는 죽지 않고, 취소 시 프로세스만 종료하면 된다.
"""
import json
import re
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

MANUAL_DOC = "수동 입력"
TYPE_LABEL = {"drawing": "도면", "manual": "매뉴얼", "data": "자료"}
SRC_LABEL = {"pos": "텍스트(위치)", "table": "표", "text": "본문 추정", "ocr": "OCR", "ocr-text": "OCR 본문",
             "sheet": "시트", "manual": "수동"}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ───────────────────────── 인덱스 ─────────────────────────
def empty_index():
    return {"generated": "", "docs": [], "parts": [], "pages": [], "titles": []}


def load_index(path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("docs", "parts", "pages", "titles"):
        d.setdefault(k, [])
    return d


def update_manifest(data_dir) -> list:
    """data_dir 안의 인덱스 JSON(최상위에 parts 목록이 있는 파일)을 찾아 manifest.json 에 기록.
    웹 조회 화면은 이 목록의 파일을 모두 불러와 합친다. (notes/manifest/*.notes.json 은 제외)"""
    d = Path(data_dir)
    files = []
    for p in sorted(d.rglob("*.json")):
        if p.name.lower() in ("notes.json", "manifest.json") or p.name.lower().endswith(".notes.json"):
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("parts"), list):
            files.append(p.relative_to(d).as_posix())
    new = {"files": files}
    mf = d / "manifest.json"
    try:
        if json.loads(mf.read_text(encoding="utf-8")).get("files") == files:
            return files                      # 변경 없으면 파일을 건드리지 않음 (불필요한 커밋 방지)
    except Exception:
        pass
    new["generated"] = datetime.now().isoformat(timespec="seconds")
    mf.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    return files


def drop_doc(idx: dict, file: str):
    idx["docs"] = [d for d in idx["docs"] if d["file"] != file]
    for k in ("parts", "pages", "titles"):
        idx[k] = [x for x in idx[k] if x.get("doc") != file]


def merge_result(idx: dict, res: dict, abs_path: str = ""):
    """worker 결과({doc, parts, pages, titles})를 인덱스에 반영(같은 문서는 교체)."""
    doc = dict(res["doc"])
    if abs_path:
        doc["abs"] = abs_path
    drop_doc(idx, doc["file"])
    idx["docs"].append(doc)
    idx["parts"].extend(res["parts"])
    idx["pages"].extend(res["pages"])
    idx["titles"].extend(res["titles"])


def unique_rel(idx: dict, rel: str, abs_path: str) -> str:
    """같은 상대경로의 다른 파일이 이미 있으면 절대경로 기반 이름으로 구분."""
    for d in idx["docs"]:
        if d["file"] == rel and d.get("abs") and Path(d["abs"]) != Path(abs_path):
            return Path(abs_path).as_posix().replace(":", "")
    return rel


def finalize(idx: dict) -> dict:
    """저장 직전 정리: 수동 항목의 문서 항목 보장, 문서별 건수 갱신."""
    files = {d["file"] for d in idx["docs"]}
    counts = {}
    for p in idx["parts"]:
        counts[p["doc"]] = counts.get(p["doc"], 0) + 1
    for p in idx["parts"]:
        if p.get("src") == "manual" and p["doc"] not in files:
            idx["docs"].append({"file": p["doc"], "type": "data", "kind": "manual", "pages": 0, "hash": "",
                                "count": 0, "ocr_pages": 0, "warnings": []})
            files.add(p["doc"])
    for d in idx["docs"]:
        d["count"] = counts.get(d["file"], 0)
    # 수동 항목만 남은 가짜 문서는 항목이 없으면 제거
    idx["docs"] = [d for d in idx["docs"] if not (d.get("kind") == "manual" and d["count"] == 0)]
    idx["generated"] = datetime.now().isoformat(timespec="seconds")
    return idx


def manual_parts(idx: dict):
    return [p for p in idx["parts"] if p.get("src") == "manual"]


def new_manual(fields: dict, pid: str = "") -> dict:
    g = lambda k: str(fields.get(k, "") or "").strip()
    try:
        page = int(g("page") or 0)
    except ValueError:
        page = 0
    return {"id": pid or "m" + uuid.uuid4().hex[:11], "doc": g("doc") or MANUAL_DOC, "page": page,
            "no": g("no"), "pn": g("pn"), "name": g("name"), "spec": g("spec"), "qty": g("qty"),
            "material": g("material"), "remark": g("remark"), "src": "manual", "title": g("title")}


def write_json(idx: dict, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def _xl(v):
    return _ILLEGAL.sub("", v) if isinstance(v, str) else v


def write_xlsx(idx: dict, path, notes: dict | None = None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    notes = notes or {}
    typ = {d["file"]: d["type"] for d in idx["docs"]}
    wb = Workbook()

    def sheet(ws, header, rows, widths):
        ws.append(header)
        for r in rows:
            ws.append([_xl(c) for c in r])
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2F5597")
            c.alignment = Alignment(vertical="center")
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Parts"
    sheet(ws, ["Part 이름", "규격", "품번", "수량", "번호", "재질", "비고", "도면 제목", "문서", "페이지", "종류", "출처", "설명(메모)"],
          [[p["name"], p["spec"], p["pn"], p["qty"], p["no"], p["material"], p["remark"], p.get("title", ""),
            p["doc"], p["page"], TYPE_LABEL.get(typ.get(p["doc"], ""), ""), SRC_LABEL.get(p.get("src", ""), p.get("src", "")),
            (notes.get(p["id"]) or {}).get("text", "")] for p in idx["parts"]],
          [38, 28, 22, 8, 7, 14, 30, 30, 40, 8, 8, 12, 40])

    ws2 = wb.create_sheet("문서(진단)")
    sheet(ws2, ["문서", "종류", "형식", "페이지/행 수", "추출 Part 수", "OCR 페이지", "경고", "원본 경로"],
          [[d["file"], TYPE_LABEL.get(d["type"], d["type"]), d.get("kind", ""), d.get("pages", 0), d.get("count", 0),
            d.get("ocr_pages", 0), "; ".join(d.get("warnings", [])), d.get("abs", "")] for d in idx["docs"]],
          [50, 8, 8, 12, 12, 10, 60, 60])

    if idx.get("pages"):
        ws3 = wb.create_sheet("페이지 글자")
        sheet(ws3, ["문서", "페이지", "도면 제목", "글자"],
              [[g["doc"], g["page"], g.get("title", ""), g["text"]] for g in idx["pages"]], [40, 8, 30, 120])
    wb.save(path)


# ───────────────────────── worker (별도 프로세스) ─────────────────────────
def worker_main(jobfile: str) -> int:
    """job JSON 을 읽어 파일 1개 인덱싱. 진행/결과는 파일로 전달(윈도우 앱은 stdout 이 없으므로)."""
    job = json.loads(Path(jobfile).read_text(encoding="utf-8"))
    ev = open(job["events"], "a", encoding="utf-8", buffering=1)

    def emit(**k):
        ev.write(json.dumps(k, ensure_ascii=False) + "\n")
        ev.flush()

    try:
        import index_pdfs as ix
        path = Path(job["path"])
        mode, pages, ft = job.get("mode", "auto"), job.get("pages", ""), bool(job.get("find_tables"))
        emit(ev="hash")
        fh = ix.file_hash(path)
        full = fh + f":{mode}:{pages}:{ft}:{ix.CACHE_VER}"
        if job.get("prev_hash") and job["prev_hash"] == full:
            emit(ev="cached")
            return 0
        ocr = ix.OCR(int(job.get("dpi", 0)))
        last = [0.0]

        def progress(done, total, msg):
            now = time.time()
            if now - last[0] > 0.15 or msg.startswith("OCR"):
                last[0] = now
                emit(ev="page", done=done, total=total, msg=msg)

        res = ix.process_file(path, Path(job["root"]), ocr, mode, pages, ft, progress=progress,
                              file_hash_value=fh, rel=job["rel"], force_type=job.get("force_type", ""))
        if ocr.available is False:
            res["doc"]["warnings"].append(f"OCR 사용 불가: {ocr.error}")
        Path(job["result"]).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        emit(ev="done", parts=len(res["parts"]))
        return 0
    except Exception:
        emit(ev="error", msg=traceback.format_exc()[-1500:])
        return 1
    finally:
        ev.close()
