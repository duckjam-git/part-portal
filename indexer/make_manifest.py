#!/usr/bin/env python
"""docs/data 안의 인덱스 JSON 목록(manifest.json)을 만든다 — 웹 조회 화면이 이 목록의 파일을 전부 불러옴.

    python indexer/make_manifest.py            # 기본: docs/data
    python indexer/make_manifest.py <폴더>

인덱스 JSON 을 추가/삭제/교체한 뒤 실행하고 커밋하세요. (.github/workflows/manifest.yml 이 push 때 자동 실행)
Part Indexer 앱이 docs/data 안에 저장하면 자동으로 갱신됩니다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import appcore  # noqa: E402

d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "data"
files = appcore.update_manifest(d)
print(f"{d / 'manifest.json'}: {len(files)}개 → " + ", ".join(files))
