#!/usr/bin/env python
"""설비 도면/매뉴얼(PDF·이미지)에서 Part 이름 / 규격 / 페이지 정보를 추출해 JSON 인덱스로 저장.

사용법:
    python index_pdfs.py <폴더 또는 파일> [--out ../docs/data/parts_index.json]
                         [--ocr auto|off|force] [--dpi 0] [--pages 1-20] [--full]

- 지원 형식: PDF, PNG, JPG, TIF(다중 페이지), BMP, GIF
- 텍스트가 있는 PDF는 바로 추출하고, 텍스트가 없는 페이지(스캔본)/이미지는 OCR(RapidOCR)로 인식
  (OCR 설치: pip install rapidocr  -- 첫 실행 때 모델 자동 다운로드)
- 폴더/파일명에 manual/매뉴얼/메뉴얼 이 있으면 type=manual, 아니면 drawing
- 변경되지 않은 파일(해시 동일)은 이전 결과를 재사용 (--full 로 강제 재분석)
- --pages 는 시험용(앞쪽 일부 페이지만). 예: --pages 1-20
"""
import argparse
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

import pymupdf

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
TEXT_EXTS = {".txt"}
TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm"}
EXTS = {".pdf"} | IMAGE_EXTS | TEXT_EXTS | TABLE_EXTS
CACHE_VER = "v5"  # 추출 알고리즘이 바뀌면 올려서 캐시 무효화 (PDF 로직 변경 없음 → 기존 인덱스 캐시 유지)

# 표 헤더 별칭 (소문자, 공백/특수문자 제거 후 비교)
HEADER_ALIASES = {
    "no": ["no", "번호", "순번", "item", "번"],  # 'ref' 는 수량 값(REF)과 겹쳐서 제외
    "name": ["품명", "명칭", "부품명", "품목", "parts", "part", "partname", "partsname", "description", "itemname", "name", "desc", "품목명"],
    "spec": ["규격", "사양", "스펙", "spec", "specification", "size", "model", "type", "형식", "모델", "치수"],
    "pn": ["품번", "부품번호", "도번", "도면번호", "p/n", "pn", "partno", "partnumber", "dwgno", "drawingno", "dwg", "자재코드", "자재번호"],
    "qty": ["수량", "qty", "q'ty", "quantity", "ea"],
    "material": ["재질", "material", "mat'l", "matl", "재료"],
    "remark": ["비고", "remark", "remarks", "note", "notes", "code", "코드"],
}
_NORM = re.compile(r"[\s\.\-_'’\(\)\[\]]")
FIELD_OF = {}
for _f, _al in HEADER_ALIASES.items():
    for _a in _al:
        FIELD_OF.setdefault(_NORM.sub("", _a.lower()), _f)
# 표가 없는 페이지(주로 매뉴얼 본문/회로도)에서 쓰는 부품번호 패턴
PN_TOKEN = re.compile(r"\b[A-Z]{1,6}[-_]?\d{2,}[A-Z0-9\-_/\.]*\b")
MIN_TEXT_CHARS = 30  # 이보다 글자가 적은 페이지는 스캔/이미지로 보고 OCR
NUMERIC = re.compile(r"^\d+(\.\d+)?[a-zA-Z]{0,3}$")


def norm(s: str) -> str:
    return _NORM.sub("", (s or "").lower())


def clean(s) -> str:
    return re.sub(r"[\s\x00]+", " ", (s or "")).strip()


def map_header(row):
    """텍스트 PDF 표의 헤더 행이면 {field: col_index} 반환, 아니면 None."""
    mapping = {}
    for i, cell in enumerate(row):
        f = FIELD_OF.get(norm(cell))
        if f and f not in mapping:
            mapping[f] = i
    if ("name" in mapping or "pn" in mapping) and len(mapping) >= 2:
        return mapping
    return None


def doc_type(path: Path) -> str:
    s = str(path).lower()
    return "manual" if any(k in s for k in ("manual", "매뉴얼", "메뉴얼", "취급설명", "사용설명")) else "drawing"


def file_hash(path: Path) -> str:
    """대용량(수 GB) 파일도 메모리에 통째로 올리지 않도록 조각 단위로 해시."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def make_id(*parts) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def mk_part(rel, pno, no, pn, name, spec, qty, material, remark, src):
    return {"id": make_id(rel, str(pno), pn, name, spec, no), "doc": rel, "page": pno, "no": no, "pn": pn,
            "name": name, "spec": spec, "qty": qty, "material": material, "remark": remark, "src": src}


# ───────────────────────── OCR ─────────────────────────
class OCR:
    """RapidOCR 지연 로딩. 설치되어 있지 않으면 available=False."""
    TARGET_PX = 4500  # dpi=0(자동)일 때 긴 변을 이 픽셀 근처로 렌더링 (A3→약 270dpi, A1→약 140dpi)

    def __init__(self, dpi: int):
        self.dpi, self.eng, self.available, self.error = dpi, None, None, ""

    def _load(self):
        if self.available is not None:
            return
        try:
            from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
            self.eng = RapidOCR(params={
                "Rec.lang_type": LangRec.KOREAN, "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Global.max_side_len": 6000,  # 큰 도면에서 작은 글자가 축소로 사라지지 않게
                # 표 안의 작은 글자(헤더 등)를 놓치지 않도록 검출 임계값을 낮춤
                "Det.thresh": 0.2, "Det.box_thresh": 0.3, "Det.unclip_ratio": 2.0, "Global.text_score": 0.3,
            })
            self.available = True
        except Exception as e:
            self.available, self.error = False, str(e)

    def page_items(self, page):
        """페이지를 렌더링해 OCR → [{x0,x1,cy,h,text,score}] (픽셀 좌표)"""
        self._load()
        if not self.available:
            return None
        import numpy as np
        dpi = self.dpi or int(max(100, min(300, self.TARGET_PX / (max(page.rect.width, page.rect.height) / 72))))
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3)
        r = self.eng(img)
        items = []
        if r is None or r.boxes is None:
            return items
        for box, txt, sc in zip(r.boxes, r.txts, r.scores):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            t = fix_text(clean(txt))
            if t:
                items.append({"x0": min(xs), "x1": max(xs), "cy": (min(ys) + max(ys)) / 2,
                              "h": max(ys) - min(ys), "text": t, "score": float(sc)})
        return items


_CODE = re.compile(r"^[A-Z0-9\-_/.Ø]+$")


def fix_text(t: str) -> str:
    """OCR 흔한 오류 보정: 'B R - 1 00 1'→'BR-1001', '<1)'→'(1)'."""
    t = re.sub(r"<(\d+)[)>]", r"(\1)", t)
    t = re.sub(r"\((\d+)>", r"(\1)", t)
    out = []
    for tok in t.split(" "):
        if out and _CODE.match(out[-1]) and _CODE.match(tok) and (len(out[-1]) <= 2 or len(tok) <= 2):
            out[-1] += tok
        else:
            out.append(tok)
    return " ".join(out)


def group_lines(items, precise=False):
    if not items:
        return []
    tol = max(statistics.median(i["h"] for i in items) * (0.45 if precise else 0.6), 3)
    lines, cur, ref = [], [], 0.0
    for it in sorted(items, key=lambda i: i["cy"]):
        # precise(텍스트 PDF): 줄의 첫 글자 높이 기준 — 평균 기준이면 페이지 다른 곳의 글자가 사슬처럼 이어 붙어 표 줄이 합쳐짐
        # OCR: 글자 상자 높이가 들쭉날쭉하므로 줄 평균 기준으로 여유 있게
        base = ref if precise else (sum(x["cy"] for x in cur) / len(cur) if cur else 0.0)
        if cur and it["cy"] - base > tol:
            lines.append(sorted(cur, key=lambda i: i["x0"]))
            cur = []
        if not cur:
            ref = it["cy"]
        cur.append(it)
    if cur:
        lines.append(sorted(cur, key=lambda i: i["x0"]))
    return lines


def center(i):
    return (i["x0"] + i["x1"]) / 2


def find_blocks(lines):
    """한 줄 안에서 헤더 단어(NO./PARTS/DWG NO./QTY…)가 반복되면 표 블록 여러 개로 분리."""
    blocks = []
    for li, line in enumerate(lines):
        cur = {}  # field -> item
        runs = []
        for it in line:
            f = FIELD_OF.get(norm(it["text"]))
            if not f:
                continue
            if f in cur:           # 같은 필드가 또 나오면 새 블록
                runs.append(cur)
                cur = {}
            cur[f] = it
        runs.append(cur)
        for run in runs:
            # BOM에는 수량(QTY) 열이 있음. 없으면 (품명+품번+4열 이상)일 때만 표로 인정
            # (도면 우측 하단 타이틀 블록의 'PARTS LIST', 'MATERIAL' 같은 글자를 BOM 헤더로 오인하는 것 방지)
            if ("name" in run or "pn" in run) and len(run) >= 3 and (
                    "qty" in run or ("name" in run and "pn" in run and len(run) >= 4)):
                cols = sorted(((f, center(i)) for f, i in run.items()), key=lambda c: c[1])
                cxs = [c[1] for c in cols]
                left = cxs[0] - 0.5 * (cxs[1] - cxs[0])
                right = cxs[-1] + 0.7 * (cxs[-1] - cxs[-2])
                blocks.append({"li": li, "cols": cols, "left": left, "right": right,
                               "cy": line[0]["cy"], "h": statistics.median(i["h"] for i in run.values())})
    # 오른쪽 경계는 바로 옆(오른쪽) 블록의 시작을 넘지 않게 제한
    for b in blocks:
        nxt = [o["left"] for o in blocks if o["left"] > b["left"] + 1]
        if nxt:
            b["right"] = min(b["right"], min(nxt) - 1)
    return blocks


def join_cells(cells):
    """{field: [박스...]} → {field: 문자열}"""
    v = {f: clean(" ".join(i["text"] for i in sorted(its, key=lambda i: i["x0"]))) for f, its in cells.items()}
    if re.match(r"^s\d+$", v.get("no", "")):
        v["no"] = v["no"].upper()
    return v


def assign_by_header(block, items):
    """1단계: 헤더 중심에 가장 가까운 열로 배정 (열 위치를 아직 모를 때)."""
    cells = {f: [] for f, _ in block["cols"]}
    for it in items:
        f = min(block["cols"], key=lambda col: abs(col[1] - center(it)))[0]
        cells[f].append(it)
    return cells


def column_stats(block, rows_cells):
    """2단계 준비: 실제 데이터 행에서 열별 글자 시작/끝/중심 위치(중앙값)를 학습.
    헤더는 가운데 정렬이고 데이터는 왼쪽 정렬인 표가 많아서 헤더 위치만으로는 어긋남."""
    acc = {f: [] for f, _ in block["cols"]}
    for cells in rows_cells:
        if sum(1 for its in cells.values() if its) >= 3:
            for f, its in cells.items():
                if its:
                    acc[f].append(min(its, key=lambda i: i["x0"]))
    stats = {}
    for f, cx in block["cols"]:
        a = acc[f]
        if len(a) >= 2:
            stats[f] = (statistics.median(i["x0"] for i in a), statistics.median(i["x1"] for i in a),
                        statistics.median(center(i) for i in a))
        else:
            stats[f] = (cx, cx, cx)
    return stats


def split_spanning(it, stats):
    """한 상자가 여러 열에 걸친 경우('45 6.3FT 7500306', '32 ROD(1)')를 열 경계에서 쪼갬.
    반환: [(text, x0, x1), ...]"""
    h = it["h"]
    bounds = sorted(s[0] for s in stats.values() if it["x0"] + 0.4 * h < s[0] < it["x1"] - 0.4 * h)
    toks = it["text"].split(" ")
    if not bounds or len(toks) < 2:
        return [(it["text"], it["x0"], it["x1"])]
    if it.get("words"):  # 텍스트 PDF: 단어마다 실제 좌표로 열을 정하고, 같은 열이 이어지는 단어끼리 묶음
        def col_of(w):
            c = (w[1] + w[2]) / 2
            return min(stats, key=lambda k: min(abs(w[1] - stats[k][0]), abs(w[2] - stats[k][1]), abs(c - stats[k][2])))
        groups = []
        for w in it["words"]:
            if groups and col_of(groups[-1][-1]) == col_of(w):
                groups[-1].append(w)
            else:
                groups.append([w])
        return [(" ".join(w[0] for w in g), g[0][1], g[-1][2]) for g in groups]
    cw = (it["x1"] - it["x0"]) / max(len(it["text"]), 1)  # 글자당 폭(근사)
    groups, cur, off, bi = [], [], 0, 0
    for tk in toks:
        x = it["x0"] + off * cw
        if bi < len(bounds) and x >= bounds[bi] - 0.5 * h and cur:
            groups.append(cur)
            cur = []
            while bi < len(bounds) and x >= bounds[bi] - 0.5 * h:
                bi += 1
        cur.append((tk, x))
        off += len(tk) + 1
    groups.append(cur)
    out = []
    for g in groups:
        x0 = g[0][1]
        out.append((" ".join(t for t, _ in g), x0, x0 + sum(len(t) + 1 for t, _ in g) * cw - cw))
    return out


def assign_by_stats(block, items, stats):
    cells = {f: [] for f, _ in block["cols"]}
    for it in items:
        for text, x0, x1 in split_spanning(it, stats):
            c = (x0 + x1) / 2
            f = min(stats, key=lambda k: min(abs(x0 - stats[k][0]), abs(x1 - stats[k][1]), abs(c - stats[k][2])))
            cells[f].append({"text": text, "x0": x0, "x1": x1})
    return cells


def lenient_ok(v):
    """1단계(열 위치를 아직 모를 때)용 느슨한 검사: 번호/수량 꼴이 어느 칸에든 있어야 함."""
    if not (v.get("name") or v.get("pn")) or sum(1 for x in v.values() if x) < 2:
        return False
    # 번호+수량+품번이 한 상자로 합쳐진 행('45 6.3FT 7500306')도 통과하도록 토큰 단위로 검사
    return any(ITEMNO.match(t) or QTYOK.match(t) for x in v.values() for t in x.split() if len(t) <= 6)


ITEMNO = re.compile(r"^[A-Za-z]{0,2}\d{1,4}[A-Za-z]?$")                         # 1, 23, S19, 12A
QTYOK = re.compile(r"^(\d+(\.\d+)?[A-Za-z]{0,3}|REF|A/R|AR|N/A)$", re.I)      # 4, 6.3FT, REF, A/R


def looks_like_bom_row(v):
    """진짜 BOM 행은 항목 번호나 수량이 숫자 꼴이다 (타이틀 블록/주석 글자 걸러내기)."""
    return bool(ITEMNO.match(v.get("no", "")) or QTYOK.match(v.get("qty", "")))


def valid_row(v, has_qty):
    filled = sum(1 for x in v.values() if x)
    if not (v.get("name") or v.get("pn")) or not looks_like_bom_row(v):
        return False
    if filled >= 3 and (v.get("name") or not has_qty):
        return True
    return filled >= 2 and bool(NUMERIC.match(v.get("qty", "")))


def text_items(page):
    """텍스트 PDF의 글자 줄(line)을 OCR 박스와 같은 형태로 변환 (위치 기반 표 복원용)."""
    lines = {}
    for x0, y0, x1, y1, w, bno, lno, _ in page.get_text("words"):
        w = w.replace("\x00", "").strip()
        if w:
            lines.setdefault((bno, lno), []).append((w, x0, x1, y0, y1))
    items = []
    for ws in lines.values():
        ws.sort(key=lambda w: w[1])
        y0, y1 = min(w[3] for w in ws), max(w[4] for w in ws)
        items.append({"x0": ws[0][1], "x1": max(w[2] for w in ws), "cy": (y0 + y1) / 2, "h": max(y1 - y0, 1),
                      "text": " ".join(w[0] for w in ws), "score": 1.0,
                      "words": [(w[0], w[1], w[2]) for w in ws]})  # 단어별 정확한 위치 (열 분리에 사용)
    return items


def page_title(items):
    """도면 타이틀 블록에서 도면 제목을 추출. 'DRAWING NO.' 행을 기준점으로 삼고,
    그 바로 위에 있는 같은 크기의 글자 줄(들)을 제목으로 본다 (예: 'ASSY RMT DI HEAT EXCH').
    TITLE 라벨 유무와 상관없이 동작하며, 제목이 두 줄이어도 이어 붙임."""
    anchors = [i for i in items if norm(i["text"]) in ("drawingno", "drawingnumber")]
    if not anchors:
        return ""
    a = max(anchors, key=lambda i: i["cy"] + i["x0"])           # 타이틀 블록은 도면 우측 하단
    h = a["h"]
    same_row = [i for i in items if abs(i["cy"] - a["cy"]) < 0.7 * h]
    size = [i for i in same_row if norm(i["text"]) == "size" and i["x0"] < a["x0"]]
    rev = [i for i in same_row if norm(i["text"]) == "rev" and i["x0"] > a["x0"]]
    left = (max(size, key=lambda i: i["x0"])["x0"] if size else a["x0"] - 4 * h) - h
    right = (min(rev, key=lambda i: i["x0"])["x1"] if rev else a["x1"] + 12 * h) + h
    cand = [i for i in items if i["cy"] < a["cy"] - 0.5 * h and left <= center(i) <= right
            and norm(i["text"]) not in ("title", "size", "rev", "scale")
            and not re.search(r"axcelis|technologies|\binc\b", i["text"], re.I)]   # 로고 옆 회사명 문구 제외
    if not cand:
        return ""
    # 기준 행 바로 위에서부터, 같은 글자 크기인 줄을 위로 거슬러 올라가며 수집
    lines = group_lines(cand, precise=True)
    lines.sort(key=lambda l: -l[0]["cy"])                       # 아래 → 위
    first_h = statistics.median(i["h"] for i in lines[0])
    if a["cy"] - lines[0][0]["cy"] > 4.5 * first_h:             # 기준 행에서 너무 멀면 제목이 아님
        return ""
    got, prev = [], None
    for l in lines:
        lh = statistics.median(i["h"] for i in l)
        if abs(lh - first_h) > 0.2 * first_h:
            break                                               # 글자 크기가 다르면(로고 문구/라벨) 중단
        if prev is not None and prev - l[0]["cy"] > 2.2 * first_h:
            break
        got.append(l)
        prev = l[0]["cy"]
    got.reverse()                                               # 위 → 아래
    title = " ".join(i["text"] for l in got for i in l)
    return clean(title)[:150]


def parts_from_ocr(items, rel, pno, src="ocr"):
    """OCR 박스에서 BOM 표를 복원. 헤더가 표 위/아래 어느 쪽에 있어도, 한 줄에 표가 여러 블록이어도 처리.
    표를 못 찾으면 품번 패턴으로 추정."""
    lines = group_lines(items, precise=(src == "pos"))
    blocks = find_blocks(lines)
    header_lis = {(b["li"], b["left"]) for b in blocks}
    parts, seen = [], set()

    for b in blocks:
        has_qty = any(f == "qty" for f, _ in b["cols"])
        gapmax = b["h"] * 3.2
        best = []   # [(원본 박스 목록, 1단계 배정)]
        for d in (1, -1):
            rows, miss, prev_cy, k = [], 0, b["cy"], b["li"] + d
            while 0 <= k < len(lines):
                line = lines[k]
                if abs(line[0]["cy"] - prev_cy) > gapmax:
                    break
                if any(hli == k and b["left"] - 10 < hl < b["right"] for hli, hl in header_lis):
                    break  # 같은 위치에 있는 다른 표의 헤더를 만나면 중단
                inb = [i for i in line if b["left"] <= center(i) <= b["right"]]
                cells = assign_by_header(b, inb) if inb else None
                if cells and lenient_ok(join_cells(cells)):
                    rows.append((inb, cells))
                    miss = 0
                else:
                    miss += 1
                    if miss >= 2:
                        break
                prev_cy, k = line[0]["cy"], k + d
            # 2단계: 데이터에서 열 위치를 학습해 재배정(+여러 열에 걸친 상자 분리) 후 엄격하게 검증
            stats = column_stats(b, [c for _, c in rows])
            fin = []
            for inb, _ in rows:
                v = join_cells(assign_by_stats(b, inb, stats))
                if valid_row(v, has_qty):
                    fin.append(v)
            if len(fin) > len(best):   # 위/아래 중 최종 검증을 통과한 행이 더 많은 쪽 선택
                best = fin
        final = best
        for v in final:
            key = (v.get("no", ""), v.get("pn", ""), v.get("name", ""), v.get("spec", ""))
            if key in seen:
                continue
            seen.add(key)
            parts.append(mk_part(rel, pno, v.get("no", ""), v.get("pn", ""), v.get("name", ""), v.get("spec", ""),
                                 v.get("qty", ""), v.get("material", ""), v.get("remark", ""), src))
    return parts


def ocr_page_text(items, limit=8000):
    """표가 없는 페이지(회로도 등)는 Part를 억지로 추정하지 않고, 페이지 전체 글자를 검색용으로 보관."""
    out, seen = [], set()
    for line in group_lines([i for i in items if i["score"] >= 0.4]):
        for it in line:
            t = it["text"]
            if len(t) >= 2 and t not in seen:
                seen.add(t)
                out.append(t)
    return " | ".join(out)[:limit]


def parts_from_line(line, rel, pno, src):
    line = clean(line)
    m = PN_TOKEN.search(line)
    if not m or len(line) > 120:
        return []
    before, after = clean(line[:m.start()]), clean(line[m.end():])
    # "이름 품번 규격/설명" 형태면 앞=이름, 뒤=규격. 앞이 없으면 뒤 전체를 이름으로.
    name, spec = (before, after) if before else (after, "")
    if not any(ch.isalpha() for ch in name + spec):
        return []
    return [mk_part(rel, pno, "", m.group(), name, spec, "", "", "", src)]


# ───────────────────────── 추출 ─────────────────────────
def parse_pages(spec, n):
    if not spec:
        return range(n)
    a, _, b = spec.partition("-")
    lo, hi = int(a), int(b or a)
    return range(max(lo, 1) - 1, min(hi, n))


def extract_file(path: Path, rel: str, ocr: OCR, mode: str, pages_spec: str = "", use_tables: bool = False,
                 progress=None):
    """progress(done, total, msg) — 페이지 단위 진행 알림(선택)."""
    parts, warnings, ocr_pages, pagetexts, titles = [], [], 0, [], []
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # 손상/보안(DRM) 파일
        return parts, 0, [f"열기 실패(보안/손상 가능): {e}"], 0, pagetexts, titles
    if doc.needs_pass:
        return parts, 0, ["비밀번호 보호 PDF - 건너뜀"], 0, pagetexts, titles

    n = len(doc)
    last_header = None      # 텍스트 PDF 표 헤더 (페이지 간 이어짐용)
    no_ocr_pages = 0
    todo = parse_pages(pages_spec, n)
    for k, pno in enumerate(todo):
        if progress:
            progress(k, len(todo), f"p.{pno + 1}/{n}")
        page = doc[pno]
        raw = page.get_text()
        text = raw.replace("\x00", "")
        # 글자 대부분이 NUL 이면 텍스트 층이 깨진 PDF(폰트 매핑 없음) → 스캔본처럼 OCR (표 탐색은 크래시 위험)
        broken = raw.count("\x00") > 0.3 * max(len(raw), 1)
        is_scan = broken or len(text.strip()) < MIN_TEXT_CHARS
        found = 0
        structured = 0  # 표에서 얻은 Part 수 (페이지 텍스트 보관 여부 판단용)
        title = ""
        p_start, pt_start = len(parts), len(pagetexts)

        if mode != "force" and not is_scan:
            # 1) 글자 위치 기반 표 복원 (선 없는 표도 가능, 빠름) — OCR과 같은 알고리즘
            titems = text_items(page)
            title = page_title(titems)
            pos = parts_from_ocr(titems, rel, pno + 1, "pos")
            if pos:
                parts.extend(pos)
                found += len(pos)
                structured += len(pos)
            # 2) 위치 기반으로 못 찾았을 때만, 옵션으로 PyMuPDF 표 탐색 (큰 도면에서 페이지당 십수 초 걸림)
            tables = []
            if not pos and use_tables:
                try:
                    tables = page.find_tables().tables
                except Exception:
                    tables = []
            for t in tables:
                rows = t.extract()
                if not rows:
                    continue
                header, start = None, 0
                for ri in range(min(3, len(rows))):
                    m = map_header(rows[ri])
                    if m:
                        header, start = m, ri + 1
                        break
                if header is None and last_header and len(rows[0]) == last_header[1]:
                    header, start = last_header[0], 0  # 이전 페이지 표의 연속
                if header is None:
                    continue
                last_header = (header, len(rows[0]))
                for row in rows[start:]:
                    get = lambda f: clean(row[header[f]]) if f in header and header[f] < len(row) else ""
                    name, spec, pn = get("name"), get("spec"), get("pn")
                    if not (name or pn) or map_header(row):
                        continue
                    parts.append(mk_part(rel, pno + 1, get("no"), pn, name, spec, get("qty"), get("material"), get("remark"), "table"))
                    found += 1
                    structured += 1
            if found == 0:  # 표가 없으면 줄 단위 품번 패턴 (정확도 낮음)
                for line in text.splitlines():
                    got = parts_from_line(line, rel, pno + 1, "text")
                    parts.extend(got)
                    found += len(got)
            if not structured and text.strip():  # 표가 없는 본문은 페이지 텍스트로도 보관 (검색용)
                pagetexts.append({"doc": rel, "page": pno + 1, "text": clean(text)[:8000]})

        # OCR: 스캔/이미지 페이지, 또는 --ocr force
        if mode != "off" and (is_scan or mode == "force"):
            if progress:
                progress(k, len(todo), f"OCR p.{pno + 1}/{n}")
            items = ocr.page_items(page)
            if items is None:
                no_ocr_pages += 1
            else:
                title = page_title(items) or title
                got = parts_from_ocr(items, rel, pno + 1)
                parts.extend(got)
                if not got:
                    pagetexts.append({"doc": rel, "page": pno + 1, "text": ocr_page_text(items)})
                ocr_pages += 1
        elif is_scan:
            no_ocr_pages += 1

        # 이 페이지의 도면 제목을 페이지의 모든 Part / 페이지 텍스트에 붙임
        if title:
            titles.append({"doc": rel, "page": pno + 1, "title": title})
        for p in parts[p_start:]:
            p["title"] = title
        for g in pagetexts[pt_start:]:
            g["title"] = title
    doc.close()

    if no_ocr_pages:
        if mode == "off":
            warnings.append(f"텍스트 없는 페이지 {no_ocr_pages}개 - OCR 꺼짐(--ocr auto 로 실행)")
        else:
            warnings.append(f"텍스트 없는 페이지 {no_ocr_pages}개 - OCR 미설치 (pip install rapidocr)")
    if ocr_pages:
        warnings.append(f"OCR 인식 {ocr_pages}페이지 - 오탈자 가능, 결과 확인 권장")
    if pages_spec:
        warnings.append(f"시험 실행: 페이지 {pages_spec} 만 처리됨")
    return parts, n, warnings, ocr_pages, pagetexts, titles


LINES_PER_PAGE = 40  # txt 는 이 줄 수마다 "페이지" 하나로 묶음


def read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp949"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", "replace")


def extract_text(path: Path, rel: str, progress=None):
    """txt: 40줄 단위 '페이지'로 나눠 글자 검색용으로 보관 + 품번 패턴 줄은 Part 로 추정."""
    lines = read_text_any(path).splitlines()
    parts, pagetexts, warnings = [], [], []
    npages = max(1, -(-len(lines) // LINES_PER_PAGE))
    for pg in range(npages):
        if progress:
            progress(pg, npages, f"p.{pg + 1}/{npages}")
        chunk = lines[pg * LINES_PER_PAGE:(pg + 1) * LINES_PER_PAGE]
        text = clean(" ".join(chunk))
        if not text:
            continue
        for ln in chunk:
            parts.extend(parts_from_line(ln, rel, pg + 1, "text"))
        pagetexts.append({"doc": rel, "page": pg + 1, "text": text[:8000], "title": ""})
    warnings.append(f"txt: {LINES_PER_PAGE}줄을 1페이지로 계산 (줄 번호 {LINES_PER_PAGE}배수 기준)")
    return parts, npages, warnings, 0, pagetexts, []


def _sheet_rows(path: Path):
    """[(시트이름, 행 목록[list[str]])] — csv/tsv/xlsx 공통."""
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        import csv
        text = read_text_any(path)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except Exception:
            dialect = csv.excel_tab if ext == ".tsv" else csv.excel
        return [(path.stem, [[clean(c) for c in r] for r in csv.reader(text.splitlines(), dialect)])]
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else clean(str(c)) for c in r])
        out.append((ws.title, rows))
    wb.close()
    return out


def extract_table(path: Path, rel: str, progress=None):
    """csv/xlsx: 헤더(품명/규격/품번/수량…)를 찾아 행마다 Part 1건. 페이지 = 엑셀 행 번호, 제목 = 시트 이름.
    헤더를 못 찾으면 '첫 칸=이름, 둘째=규격, 나머지=비고'로 보관."""
    parts, warnings, titles = [], [], []
    try:
        sheets = _sheet_rows(path)
    except Exception as e:
        return parts, 0, [f"열기 실패: {e}"], 0, [], titles
    total_rows = 0
    for si, (sheet, rows) in enumerate(sheets):
        if progress:
            progress(si, len(sheets), f"시트 {sheet}")
        total_rows += len(rows)
        head_i, mapping = None, None
        for i, r in enumerate(rows[:20]):
            m = map_header(r)
            if m:
                head_i, mapping = i, m
                break
        before = len(parts)
        if mapping:
            used = set(mapping.values())
            head = rows[head_i]
            for ri in range(head_i + 1, len(rows)):
                r = rows[ri]
                get = lambda f: r[mapping[f]] if f in mapping and mapping[f] < len(r) else ""
                name, pn = get("name"), get("pn")
                if not (name or pn):
                    continue
                extra = [f"{head[c]}: {v}" for c, v in enumerate(r) if c not in used and v and c < len(head) and head[c]]
                remark = " / ".join(x for x in [get("remark")] + extra if x)
                p = mk_part(rel, ri + 1, get("no"), pn, name, get("spec"), get("qty"), get("material"), remark, "sheet")
                p["title"] = sheet
                parts.append(p)
        else:
            for ri, r in enumerate(rows):
                cells = [c for c in r if c]
                if not cells:
                    continue
                p = mk_part(rel, ri + 1, "", "", cells[0], cells[1] if len(cells) > 1 else "", "", "",
                            " | ".join(cells[2:]), "sheet")
                p["title"] = sheet
                parts.append(p)
            if len(parts) > before:
                warnings.append(f"시트 '{sheet}': 헤더(품명/규격/품번 등)를 못 찾아 첫 칸=이름으로 보관")
        if len(parts) > before:
            titles.append({"doc": rel, "page": 1, "title": sheet})
    return parts, total_rows, warnings, 0, [], []


def doc_kind(path: Path) -> str:
    e = path.suffix.lower()
    return "pdf" if e == ".pdf" else "image" if e in IMAGE_EXTS else "text" if e in TEXT_EXTS else "table"


def process_file(path: Path, root: Path, ocr: OCR, mode="auto", pages_spec="", find_tables=False,
                 progress=None, file_hash_value=None, rel=None, force_type=""):
    """파일 1개를 인덱싱해 {doc, parts, pages, titles} 반환 (형식별 자동 분기).
    force_type: ''(자동 판별) | drawing | manual | data"""
    rel = rel or path.relative_to(root).as_posix()
    h = (file_hash_value or file_hash(path)) + f":{mode}:{pages_spec}:{find_tables}:{CACHE_VER}"
    ext = path.suffix.lower()
    if ext in TABLE_EXTS:
        p, n, warn, op, pt, tt = extract_table(path, rel, progress)
    elif ext in TEXT_EXTS:
        p, n, warn, op, pt, tt = extract_text(path, rel, progress)
    else:
        p, n, warn, op, pt, tt = extract_file(path, rel, ocr, mode, pages_spec, find_tables, progress)
    kind = doc_kind(path)
    typ = doc_type(Path(root.name) / rel)  # 폴더 이름(예: Manuals)도 종류 판별에 사용
    if kind in ("text", "table") and typ == "drawing":
        typ = "data"  # 도면/매뉴얼이 아닌 일반 자료(목록·메모 등)
    if force_type in ("drawing", "manual", "data"):
        typ = force_type
    doc = {"file": rel, "type": typ, "kind": kind, "pages": n, "hash": h,
           "count": len(p), "ocr_pages": op, "warnings": warn}
    return {"doc": doc, "parts": p, "pages": pt, "titles": tt}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="PDF/이미지가 있는 폴더 또는 파일")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs" / "data" / "parts_index.json"))
    ap.add_argument("--ocr", choices=["auto", "off", "force"], default="auto",
                    help="auto: 텍스트 없는 페이지만 OCR(기본) / off: OCR 안 함 / force: 모든 페이지 OCR")
    ap.add_argument("--dpi", type=int, default=0, help="OCR 렌더링 해상도 (0=자동: 용지 크기에 맞춤)")
    ap.add_argument("--pages", default="", help="시험용: 앞쪽 일부 페이지만 (예: 1-20)")
    ap.add_argument("--find-tables", action="store_true", help="위치 기반 복원 실패 시 PyMuPDF 표 탐색도 시도 (느림)")
    ap.add_argument("--full", action="store_true", help="캐시 무시하고 전체 재분석")
    a = ap.parse_args()

    src = Path(a.input).resolve()
    out = Path(a.out)
    if src.is_file():
        root, files = src.parent, [src]
    elif src.is_dir():
        root, files = src, sorted(p for p in src.rglob("*") if p.suffix.lower() in EXTS)
    else:
        sys.exit(f"경로가 없습니다: {src}")

    old_docs, old_parts, old_pages, old_titles = {}, {}, {}, {}
    if out.exists() and not a.full:
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            old_docs = {d["file"]: d for d in old.get("docs", [])}
            for p in old.get("parts", []):
                old_parts.setdefault(p["doc"], []).append(p)
            for p in old.get("pages", []):
                old_pages.setdefault(p["doc"], []).append(p)
            for p in old.get("titles", []):
                old_titles.setdefault(p["doc"], []).append(p)
        except Exception:
            pass

    ocr = OCR(a.dpi)
    docs, parts, pages, titles = [], [], [], []
    for i, path in enumerate(files, 1):
        rel = path.relative_to(root).as_posix()
        fh = file_hash(path)
        h = fh + f":{a.ocr}:{a.pages}:{a.find_tables}:{CACHE_VER}"
        prev = old_docs.get(rel)
        if prev and prev.get("hash") == h:
            docs.append(prev)
            parts.extend(old_parts.get(rel, []))
            pages.extend(old_pages.get(rel, []))
            titles.extend(old_titles.get(rel, []))
            print(f"[{i}/{len(files)}] (캐시) {rel}")
            continue
        print(f"[{i}/{len(files)}] {rel}", flush=True)
        r = process_file(path, root, ocr, a.ocr, a.pages, a.find_tables, file_hash_value=fh,
                         progress=lambda d, t, m: print(f"    {m}", flush=True) if m.startswith("OCR") else None)
        pages.extend(r["pages"])
        titles.extend(r["titles"])
        docs.append(r["doc"])
        parts.extend(r["parts"])
        warn = r["doc"]["warnings"]
        print(f"    → {len(r['parts'])}건" + (f"  ⚠ {'; '.join(warn)}" if warn else ""))
    if ocr.available is False:
        print(f"\n[OCR 사용 불가] {ocr.error}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "docs": docs, "parts": parts, "pages": pages, "titles": titles,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n완료: 문서 {len(docs)}개 / Part {len(parts)}건 / 페이지 텍스트 {len(pages)}건 → {out}")


if __name__ == "__main__":
    main()
