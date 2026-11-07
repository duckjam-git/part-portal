@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 필요한 패키지 설치...
python -m pip install --quiet pymupdf rapidocr onnxruntime openpyxl pyinstaller
echo [2/3] OCR 모델 확인(없으면 1회 다운로드)...
python -c "from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion; RapidOCR(params={'Rec.lang_type': LangRec.KOREAN, 'Rec.model_type': ModelType.MOBILE, 'Rec.ocr_version': OCRVersion.PPOCRV5}); print('OCR 모델 OK')"
echo [3/3] exe 빌드...
python -m PyInstaller PartIndexer.spec --noconfirm --clean
echo.
echo 완료: dist\PartIndexer\PartIndexer.exe  (dist\PartIndexer 폴더째 복사해서 사용)
pause
