# guide 폴더 통일 패치 — 적용 안내

작성일: 2026-08-03. 이 zip은 레포 루트 기준 경로 구조를 따른다. `data/guides/` → `data/guide/` 통일에 필요한 코드·문서 수정 4개 파일이 들어 있다.

## 포함 파일 (4개)

| 파일 | 변경 내용 |
|---|---|
| `app/core/config.py` | `GUIDES_DIR`(data/guides) 설정 삭제, `GUIDE_DIR`(data/guide)로 통합. 주석에 세 가지 역할(시드 적재·관리자 업로드·files 모드 검색)과 통일 이력 명기 |
| `scripts/seed_docs.py` | `settings.GUIDES_DIR` → `settings.GUIDE_DIR` 2곳(적재·중단 메시지), docstring 경로 `data/guides/*` → `data/guide/*` 1곳 |
| `README_MVP.md` | 구 스크립트명 `scripts.seed_laws` 잔존 참조 3곳(11·88·101행)을 `scripts.seed_docs`로 수정 |
| `data/laws/_datalaws_수집_정제_이력.md` | v3 — 가이드 폴더 통일 반영, 시드 검증값 2건 정정: 폐기물관리법 시행령 64→66(제36217호 현행 조문 갱신분), 분리수거 지침 16→15(제11조 '삭제' 조문은 괄호가 없어 청킹 미분리) |

## 적용 순서

- [ ] 1. **폴더 이름 변경 먼저**: 레포 루트에서 `git mv data/guides data/guide` (zip으로는 이름 변경을 전달할 수 없음)
- [ ] 2. zip을 레포 루트에서 압축 해제하여 4개 파일 덮어쓰기 (`git diff`로 변경분 확인 권장)
- [ ] 3. 팀원 공지: 로컬 `.env`에 `GUIDES_DIR`를 지정해 둔 사람이 있으면 해당 라인 삭제 (pydantic 설정 필드가 없어졌으므로)
- [ ] 4. `python -m scripts.seed_docs` 재실행 → 가이드 4개 파일 적재 확인, 조문 수를 이력 v3 검증값(99·15·86·66·66·140·1·8·52)과 대조
- [ ] 5. `python -m scripts.eval_retrieval` 골든셋 실행 → 전 문항 PASS 확인
- [ ] 6. 커밋 (권장 메시지: `가이드 폴더 data/guide로 통일: GUIDES_DIR 설정 통합, seed_docs·README_MVP 참조 수정, 이력 v3`)

## 동작 변화 유의사항

- 통일 후 관리자 업로드 문서(`data/guide` 저장)가 다음 시드 실행 시 함께 적재된다. 의도된 통합이면 정상.
- `RAG_SOURCE=files` 테스트 모드도 같은 폴더를 읽게 된다. 현재 가이드 4개 파일은 모두 `[가이드]_` 접두 규칙을 따르므로 유형·지역 추출에 문제없음.
- 검증값 정정 2건은 이번 통일과 무관하게 기존 이력의 오기였음(레포 청킹 정규식 `ARTICLE_SPLIT` 기준으로 재검증한 값).
