# MVP 실행 가이드 — 법령 기반 RAG 챗봇

목표: **브라우저에서 질문하면 조문을 근거로 답하고 출처를 보여준다.**

---

## 1. 변경된 파일

### 신규
```
scripts/seed_docs.py          법령 적재 + 인덱스 생성 + 계정 생성
scripts/law_text.py           PDF·txt 추출 및 정제 (머리말·페이지번호 제거)
scripts/create_user.py        로그인 계정 생성 (회원가입 화면 대용)
data/laws/*                   법령 원문 (샘플 포함 — 실제 조문으로 교체 필요)
data/laws/README.md           법령 파일 작성 규칙
```

### 수정
```
app/models.py                 SourceType 에 law 추가 (1줄)
app/core/config.py            LAWS_DIR · RAG_MIN_SCORE · RAG_MIN_SCORE_LOCAL · GEMINI_MODEL
app/services/chunk_service.py 조문 단위 청킹
app/services/rag_service.py   공용 법령 필터 · 유사도 임계값 · 조문 인용
app/services/gemini_service.py 실제 Gemini 호출 + Few-shot 프롬프트
app/routers/rag.py            로그인 사용자 연결 (owner_id)
requirements.txt              google-genai
.env / .env.example           GEMINI_API_KEY 등
frontend/.env                 VITE_USE_MOCK=false
```

`app/main.py` 는 이미 `rag.router` 를 등록하고 있어 **수정 없음**.

---
## 2. 실행 순서

```bash
# 1) 패키지
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# 2) DB 재생성 — SourceType 에 law 가 추가되어 기존 테이블과 맞지 않는다
#    (sqlite 기준. MySQL이면 스키마 DROP 후 재생성)
del test.db          # Windows
# rm test.db         # Mac/Linux

# 3) 법령 적재 + 인덱스 생성

# .env에 SECRET_KEY= 랜덤 값
python -m scripts.seed_docs

# 4) 서버
python -m uvicorn app.main:app --reload

```
### 로그인 계정

프론트에 **회원가입 화면이 없으므로** 시드 스크립트가 계정을 만들어 준다.
3번 단계를 실행하면 마지막에 아래 계정이 출력된다.

```
이메일   : demo@example.com     ← 로그인 화면 기본값과 동일
비밀번호 : demo1234
```

다른 계정이 필요하면:

```bash
python -m scripts.create_user hong@test.com 1234 홍길동
```

이미 있는 이메일이면 비밀번호가 변경된다.
Swagger(`/docs`)의 `POST /api/auth/register` 로 만들어도 된다.

> Vite 프록시(`/api` → `localhost:8000`)가 이미 설정되어 있어 CORS 설정은 필요 없다.

---

## 3. Gemini 연결 (권장)

`.env` 에 키를 넣으면 답변 품질이 달라진다.

```
GEMINI_API_KEY=발급받은키
EMBEDDING_BACKEND=gemini
```

> ⚠️ **임베딩 백엔드를 바꾸면 벡터 차원이 384 → 768 로 달라진다.**
> 반드시 `python -m scripts.seed_docs` 를 다시 실행할 것.

**키가 없어도 서버는 뜬다.** 답변 자리에 검색된 조문 원문이 그대로 나온다.

---

## 4. 실제 법령으로 교체

현재 `data/laws/` 의 `[샘플]…` 파일은 **동작 확인용**이다. 시연 전에 교체할 것.

1. 국가법령정보센터(law.go.kr)에서 법령 검색
2. 조문 본문 복사
3. `자원순환기본법.txt` 처럼 저장 (**파일명이 곧 문서 제목**, UTF-8)
4. `[샘플]…` `테스트_…` 파일 삭제 후 `python -m scripts.seed_docs` 재실행

조문은 `제1조(목적)` 형식을 지켜야 조문 단위 청킹이 동작한다.

### PDF도 그대로 넣으면 된다

`.txt` `.md` `.pdf` 를 모두 지원한다. PDF는 적재 시 자동으로 정제된다.

| 처리 | 내용 |
|---|---|
| 머리말·꼬리말 제거 | 전체 페이지의 80% 이상에 반복되는 40자 이하 줄을 삭제 |
| 페이지번호 제거 | `- 1 -`, `1/23`, `Page 1` 등 |
| 줄바꿈 정리 | 조문·항·호로 시작하는 줄에서만 문단을 나누고 나머지는 이어 붙임 |

적재 시 **인식된 조문 수가 출력**되므로 추출 성공 여부를 바로 알 수 있다.

```
읽음: 폐기물관리법.pdf — 조문 32개, 48,210자
[경고] 지자체안내.pdf: 조문(제N조)을 찾지 못했습니다.
```

`조문 0개` 경고가 뜨면 조문 형식이 다르거나 추출이 실패한 것이다.
스캔 이미지 PDF는 OCR이 필요하므로 텍스트로 다시 구하는 편이 빠르다.

---

## 5. 동작 원리 (보고서용)

### 조문 단위 청킹
법령을 700자로 기계적으로 자르면 조문 경계가 뭉개져 정확한 인용이 불가능하다.
`제N조(제목)` 패턴 앞에서 분할해 **조문 하나 = 청크 하나**로 만들었다.
그 결과 검색 결과가 곧 인용 단위가 되어 "폐기물관리법 제15조에 따라"라고 답할 수 있다.

### 공용 문서 필터
법령은 `source_type="law"` 로 저장되어 **소유자와 무관하게 모든 사용자가 검색**한다.
개인 문서는 본인 것만 검색된다.

```python
r["owner_id"] == 내_id or r["source_type"] == "law"
```

### 환각 방지 3중 장치

| 장치 | 구현 위치 |
|---|---|
| ① 유사도 임계값 미달 시 LLM 호출 없이 "관련 조문을 찾을 수 없습니다" | `rag_service.search()` |
| ② 프롬프트에 "근거에 없으면 지어내지 말라" + Few-shot 예시 2개 | `gemini_service.ANSWER_PROMPT` |
| ③ 모든 답변에 근거 조문 출처 표시 | `rag_service._build_sources()` |

Few-shot 예시 중 하나는 **일부러 "관련 조문을 찾을 수 없습니다"로 답하는 예시**다.
자료에 없는 질문에 지어내지 않도록 학습시키는 장치다.

### 임계값을 백엔드별로 나눈 이유
local 해시 임베딩은 표면 문자열 일치만 잡아 점수 스케일이 낮다(0.05~0.15).
Gemini 임베딩(0.5~0.8)과 같은 임계값을 쓰면 local 에서는 모든 결과가 걸러진다.

---

## 6. 확인 방법 (서버만 · 프론트 없이)

http://localhost:8000/docs 에서:

1. `POST /api/auth/register` → 계정 생성
2. `POST /api/auth/login` → 세션 쿠키 발급
3. `POST /api/rag/search` → `{"query": "대형폐기물", "top_k": 3}` 로 검색 품질 확인
4. `POST /api/chat` → `{"question": "대형폐기물은 어떻게 버리나요?"}`

`POST /api/rag/rebuild` 는 문서를 추가·수정한 뒤 호출한다.

---

## 7. MVP 완료 기준

- [ ] 브라우저에서 로그인(demo@example.com / demo1234) → 질문 → 답변 + 출처 표시
- [ ] 답변에 조문 번호가 인용됨 ("○○법 제N조에 따르면…")
- [ ] 법령에 없는 질문에 지어내지 않고 "관련 조문을 찾을 수 없습니다"
- [ ] 실제 법령 2~3건으로 교체 완료

---

## 8. 다음 단계

| 순서 | 작업 |
|---|---|
| 1 | 실제 법령으로 교체 + Gemini 연결 |
| 2 | 정답셋 20문항 작성 → 검색 정확도·환각률 측정 |
| 3 | LangChain 전환 (요구사항 필수) |
| 4 | 가이드 문서(환경부·지자체) 추가 → 2층 답변 |
| 5 | 문서 저장·STT 완료 시 자동 rebuild |
| 6 | 프론트 회원가입 화면 (현재는 스크립트로 계정 생성) |
