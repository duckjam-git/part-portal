# -*- mode: python ; coding: utf-8 -*-
# 빌드: build_exe.bat  (또는 pyinstaller PartIndexer.spec --noconfirm)
# 결과: dist/PartIndexer/PartIndexer.exe  — 이 폴더째 복사하면 Python 없는 PC에서도 실행됨
from PyInstaller.utils.hooks import collect_all

datas = [("docs/index.html", "docs"), ("docs/app.html", "docs")]   # 앱 화면 + 검색 화면. 인덱스/PDF 는 넣지 않음(기밀 보호)
binaries, hiddenimports = [], ["index_pdfs", "appcore", "serve", "openpyxl"]
for pkg in ("rapidocr", "onnxruntime"):         # OCR 엔진 + 모델(.onnx) 을 통째로 포함 → 오프라인 동작
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["part_indexer_app.py"],
    pathex=[".", "indexer"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "torch", "tensorflow", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "notebook", "pytest", "pymupdf4llm", "pymupdf_layout"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PartIndexer", console=False, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PartIndexer")
