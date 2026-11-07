# Part 포털

설비 도면/매뉴얼 PDF → Part 이름·규격·페이지 인덱스 → 웹 검색 + 설명 메모

## ★ 단독 실행 앱 (Part Indexer) — Python 없이 사용
**배포본**: `dist\PartIndexer\` 폴더째 다른 PC에 복사 → `PartIndexer.exe` 실행 (설치·인터넷 불필요, 약 320MB. OCR 모델 포함)
- 화면은 웹 UI(`docs/app.html`)를 Edge/Chrome **앱 창**으로 띄움 (Windows 10/11 기본 Edge 로 충분). 창을 닫으면 앱도 종료(인덱싱 중이면 끝난 뒤 종료). 이미 실행 중이면 같은 앱의 창만 다시 열림
- 내부 서버는 `127.0.0.1`(이 PC)에서만 접속되고, 실행마다 바뀌는 토큰이 있어야 조작 가능 (다른 웹사이트의 위조 호출 차단)

1. **① 인덱싱**: `파일 추가…` / `폴더 추가…` (PDF · PNG/JPG/TIF/BMP/GIF · txt · csv · xlsx) → 옵션 선택 → `Indexing`
   - 출력 파일 이름은 `저장 위치·이름…` 으로 지정 (JSON 은 항상 저장, Excel 은 같은 이름의 `.xlsx` 로 함께 저장)
   - `이어서 추가·갱신` 체크 시 바뀌지 않은 파일은 건너뜀. 진행률·경과시간·`취소` 지원
   - 파일마다 별도 프로세스로 실행 → 특정 파일이 라이브러리를 비정상 종료시켜도 앱은 유지되고 "실패"로 표시
2. **② 수동 입력**: 자동으로 못 읽는 항목을 직접 입력/수정/삭제 (문서·페이지를 쓰면 검색 화면에서 원본 페이지로 연결). 저장하면 인덱싱 결과와 함께 검색됨
3. **③ 결과 / 진단**: 문서별 페이지·추출 건수·OCR 페이지·경고 표 (노란 줄 = 0건/실패)
4. **🔍 검색 화면 열기**: 내장 서버(이 PC에서만 접속)로 웹 검색 화면을 브라우저에 띄움
   - `진단` 버튼: 문서별 추출 현황 / 항목 클릭 → `이 페이지 항목 보기` 로 원본 PDF 페이지와 대조
   - `PDF 해당 페이지 열기`: 원본 PDF 를 해당 페이지로 열기 (원본 파일이 인덱싱 때 위치에 있어야 함)
   - 설명(메모)은 `<출력이름>.notes.json` 에 자동 저장
- txt/csv/xlsx: 헤더(품명·규격·품번·수량·재질·비고 등)를 자동 인식해 행마다 Part 1건 (페이지 = 행 번호, 제목 = 시트 이름). 헤더가 없으면 첫 칸=이름. txt 는 40줄을 1페이지로 계산해 글자 검색 + 품번 패턴 줄 추정
- **다시 빌드**: `build_exe.bat` (Python + 인터넷 필요, 수 분). 개발 중 실행: `python part_indexer_app.py`
- 참고: PyInstaller exe 는 사내 백신이 의심 파일로 오인할 수 있음 → 보안팀에 예외 등록 요청. 기밀 도면의 인덱스/PDF 는 공개 저장소에 올리지 마세요.

## ★ GitHub Pages 로 조회 페이지 올리기
`docs/` 폴더가 그대로 사이트입니다 (`docs/.nojekyll` 포함).
1. 저장소 생성 → 이 프로젝트 푸시 (`.gitignore` 가 원본 PDF·빌드 산출물·`Part_Data/` 는 제외)
2. Settings → Pages → Branch `main` / 폴더 `/docs` → 주소 `https://<계정>.github.io/<저장소>/`
3. 인덱스 올리기: Part Indexer 에서 만든 JSON 을 **`docs/data/` 에 원하는 이름으로 넣고** 커밋·푸시 → 페이지가 `docs/data/` 의 **모든 인덱스 JSON 을 자동으로 불러와 합침**
   - 정적 호스팅은 폴더 목록을 읽을 수 없어 `docs/data/manifest.json`(파일 목록)을 사용: push 시 `.github/workflows/manifest.yml` 이 자동 갱신 (Actions 를 못 쓰면 `python indexer/make_manifest.py` 실행 후 커밋. Part Indexer 앱이 `docs/data/` 에 저장해도 자동 갱신)
   - 제외되는 파일: `notes.json`, `*.notes.json`, `manifest.json`, 인덱스 형식(`parts` 목록)이 아닌 JSON
   - JSON 이 없거나 `?data=다른파일.json` 이 틀리면 "인덱스 파일 선택" 화면이 나오고, 내 PC 의 JSON(여러 개면 합쳐서)을 열 수 있음
   - Edge/Chrome 은 마지막으로 연 파일을 기억해 `↻ 마지막 파일 다시 열기` 한 번으로 재접속 (파일은 서버로 전송되지 않음)
4. 설명(메모): 각자 브라우저에 저장 → `설명 내보내기` 로 받은 `notes.json` 을 `docs/data/notes.json` 에 반영하면 모두에게 공유
5. PDF 링크: Pages 에는 PDF 가 없으므로 기본은 링크 숨김. 상세 패널의 `PDF 경로` 에 사내 서버/공유 폴더 주소를 넣으면 `열기` 가 나타나고, `📋 위치 복사` 로 문서명·페이지를 복사해 직접 열 수도 있음
- ⚠ **공개 저장소의 Pages 는 누구나 볼 수 있습니다.** 올리는 JSON 에 기밀 표시 도면(Axcelis 등)의 품번·품명이 들어 있지 않은지 확인하세요.

## 사용법 (명령줄 / GitHub Pages)
1. `pip install pymupdf`
2. 인덱싱: `python indexer/index_pdfs.py <PDF폴더>`  → `docs/data/parts_index.json` 생성
   - 폴더/파일명에 `manual`/`매뉴얼`/`메뉴얼` 이 있으면 매뉴얼, 아니면 도면으로 분류
   - 바뀐 PDF만 재분석 (`--full` 로 전체 재분석)
3. 웹: `docs/index.html` (로컬 확인: `python -m http.server 8080 --directory docs`)
4. GitHub Pages: Settings → Pages → Branch `main`, 폴더 `/docs`

## 이미지 / 스캔본 (OCR)
- PDF 외 PNG·JPG·TIF·BMP·GIF 도 인덱싱 (폴더에 같이 넣으면 됨)
- 텍스트 없는 PDF 페이지·이미지는 자동 OCR (`pip install rapidocr`, 첫 실행 때 한글 모델 자동 다운로드)
- 옵션: `--ocr auto|off|force`, `--dpi 0`(자동: 용지 크기에 맞춤), `--pages 1-20`(시험용 일부 페이지)
- BOM 표 복원: 헤더(NO./PARTS/DWG NO./QTY/MATERIAL 등)가 표 위·아래 어디에 있어도, 한 줄에 표가 여러 블록이어도 처리
- 표가 없는 페이지(회로도 등)는 Part를 추정하지 않고 페이지 글자를 보관 → 웹에서 `페이지 글자` 결과로 검색
- OCR 결과는 웹에서 `OCR` 태그로 표시되며 오탈자/열 구분 오류가 있을 수 있음
- 텍스트 층이 깨진 PDF(글자가 NUL로 추출)는 자동으로 OCR 처리 (표 탐색 크래시 방지)

## 도면 제목(Title)
- 타이틀 블록의 `DRAWING NO.` 행 바로 위 글자를 도면 제목으로 추출 (예: `ASSY RMT DI HEAT EXCH`)
- 모든 Part의 `title` 필드와 `titles`(페이지별 제목 목록)에 저장되고, 웹에서 검색/표시됨

## 설명(메모)
- Part 클릭 → 우측 패널에서 작성, 이 브라우저에 자동 저장
- 팀 공유: **설명 내보내기** → 받은 `notes.json` 을 `docs/data/notes.json` 에 덮어쓰고 커밋
- 설명은 인덱스와 분리되어 있어, 재인덱싱해도 유지됨 (Part ID = 문서+페이지+품번+이름+규격 해시)

## PDF 링크 (로컬 서버)
`python serve.py` → http://127.0.0.1:8080 에서 웹 UI와 원본 PDF(`Part_Data/` → `/pdfs/`)를 함께 제공.
Part 클릭 → "PDF 해당 페이지 열기" 가 `/pdfs/<문서>#page=N` 으로 열림. 1GB+ PDF도 필요한 구간만 전송(HTTP Range).
- 옵션: `--pdf-dir <폴더>`, `--port 9000`, `--open`(브라우저 자동 실행)
- 기본은 내 PC(127.0.0.1)에서만 접속. 다른 PC에서도 보려면 `--host 0.0.0.0` (보안 정책 확인 후)
- PDF를 다른 곳(공유 폴더/사내 서버)에 두면 웹의 "PDF 경로" 칸에 그 주소를 입력
- 기밀 도면이면 PDF/인덱스를 공개 저장소에 올리지 마세요.
