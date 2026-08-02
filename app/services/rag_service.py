# path : app/services/rag_service.py
"""
[RAG 파트] 청킹·임베딩·벡터 검색·답변 생성을 조합하는 오케스트레이터.

  - rebuild_index() : 문서 전체 → 청킹 → 임베딩 → FAISS 재구축
  - search()        : 질문과 유사한 청크 검색 (소유자 필터 + 유사도 임계값)
  - ask()           : 검색 결과를 근거로 답변 + 출처 반환

문서 출처는 .env 의 RAG_SOURCE 로 전환한다.
  - "db"    : documents 테이블 (기본)
  - "files" : data/docs 폴더의 txt/md/pdf (DB 없이 단독 테스트용)

공용 문서:
  법령(source_type="law")은 소유자와 무관하게 모든 사용자가 검색할 수 있다.
  개인 문서는 본인 것만 검색된다.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.services import chunk_service, embedding_service, vector_store_service

# 소유자와 무관하게 전체 공개되는 문서 유형 (수집한 공공자료)
PUBLIC_SOURCE_TYPES = ("law", "guide")

# 프론트에 돌려줄 근거 미리보기 길이
SNIPPET_LENGTH = 140

# 근거가 없을 때 쓰는 문구. 프롬프트의 지시문과 같은 표현을 쓴다.
NO_ANSWER = "관련 자료를 찾을 수 없습니다"


def _effective_min_score(min_score: float | None) -> float:
    """유사도 임계값을 결정한다.

    local 임베딩은 해시 기반이라 점수 스케일이 gemini 와 다르다.
    (local 0.05~0.15 / gemini 0.5~0.8 수준)
    같은 임계값을 쓰면 local 에서는 모든 결과가 걸러지므로 분리한다.
    """
    if min_score is not None:
        return min_score
    if settings.EMBEDDING_BACKEND.lower() == "local":
        return settings.RAG_MIN_SCORE_LOCAL
    return settings.RAG_MIN_SCORE


# ─────────────────── 공개 API ───────────────────


def rebuild_index(db=None) -> dict:
    """문서 전체를 다시 인덱싱한다.

    전체 재구축 방식을 쓰는 이유:
      문서 수정·삭제 시 FAISS 와의 동기화 문제를 피하는 가장 단순한 방법이다.
      문서량이 적은 초기 단계에서는 몇 초면 끝나므로 증분 갱신은 추후 과제로 둔다.
    """
    documents = _load_documents(db)
    chunks = chunk_service.build_chunks(documents)

    vectors = embedding_service.embed_documents([c["content"] for c in chunks])
    count = vector_store_service.rebuild(
        chunks, vectors, embedding_service.get_dimension()
    )

    return {
        "documents": len(documents),
        "indexed_chunks": count,
        "source": settings.RAG_SOURCE,
        "embedding_backend": settings.EMBEDDING_BACKEND,
    }


def search(
    query: str,
    top_k: int | None = None,
    owner_id: int | None = None,
    min_score: float | None = None,
    region: str | None = None,
) -> list[dict]:
    """질문과 유사한 청크를 점수 순으로 반환한다.

    owner_id 를 넘기면 "본인 문서 + 공용 법령"만 남긴다.
    region 을 넘기면 "해당 지역 + common(공통)" 문서만 남긴다.
    min_score 미만인 결과는 근거로 삼기에 부족하다고 보고 제외한다.
    """
    top_k = top_k or settings.RAG_TOP_K
    min_score = _effective_min_score(min_score)

    query_vector = embedding_service.embed_query(query)

    # 필터링 후에도 top_k 개를 채우기 위해 넉넉히 검색한 뒤 잘라낸다.
    fetch_k = top_k * 5 if (owner_id is not None or region) else top_k * 2
    results = vector_store_service.search(query_vector, fetch_k)

    if owner_id is not None:
        results = [
            r for r in results
            if r.get("owner_id") == owner_id
            or r.get("source_type") in PUBLIC_SOURCE_TYPES
        ]

    # 지역 필터: 해당 지역 + common(공통) 문서만 남긴다
    if region:
        results = [
            r for r in results
            if r.get("region") in (region, "common")
        ]

    # 유사도 임계값 (환각 방지 1차 장치)
    results = [r for r in results if r.get("score", 0.0) >= min_score]

    return results[:top_k]


def ask(
    question: str,
    top_k: int | None = None,
    owner_id: int | None = None,
    region: str | None = None,
) -> dict:
    """검색된 문맥을 근거로 3섹션 답변을 생성한다.

    반환 형식:
        {"guide": str, "law": str, "tip": str, "source": str,
         "sources": [{"document_id": int, "title": str, "snippet": str}, ...]}
    """
    results = search(question, top_k, owner_id, region=region)

    # 근거가 없으면 LLM을 호출하지 않는다. (환각 방지)
    if not results:
        return {
            "answer": "관련 문서를 찾을 수 없습니다. 질문을 조금 더 구체적으로 바꿔 보세요.",
            "tip": "",
            "source": "",
            "sources": [],
        }

    sections = _generate_answer(question, _build_context(results))
    source_list = _build_sources(results)

    return {
        "answer": sections.get("answer", ""),
        "tip": sections.get("tip", ""),
        "source": ", ".join(dict.fromkeys(s["title"] for s in source_list)),
        "sources": source_list,
    }


# ─────────────────── 컨텍스트·출처 조립 ───────────────────


def _build_context(results: list[dict]) -> str:
    """검색된 청크를 LLM에 넘길 하나의 문자열로 조립한다.

    가이드와 법령을 나눠서 넘긴다. 그래야 LLM이
    "실천 방법(가이드) + 법적 근거(법령)" 두 층으로 답할 수 있다.

        ### 배출 가이드
        [[서울시] 분리배출 요령 품목별 분리배출 요령 > 종이류]
        ...본문...

        ### 관련 법령
        [자원순환기본법 제15조]
        ...본문...
    """
    guides: list[str] = []
    laws: list[str] = []

    for item in results:
        # 청크 본문이 이미 "제8조(…)" 또는 "[품목별 요령 > 종이류]" 로 시작하므로
        # 여기서는 문서 제목만 붙인다. (라벨을 또 쓰면 중복된다)
        block = f"[{item.get('title', '제목 없음')}]\n{item['content']}"
        (laws if item.get("source_type") == "law" else guides).append(block)

    parts: list[str] = []
    if guides:
        parts.append("### 배출 가이드\n" + "\n\n".join(guides))
    if laws:
        parts.append("### 관련 법령\n" + "\n\n".join(laws))

    return "\n\n".join(parts)


def _clean_title(raw_title: str) -> str:
    """파일명 형태의 제목을 사람이 읽기 좋은 형태로 정리한다.

    예) [가이드]_환경부_공통_분리배출_기준 → 환경부 공통 분리배출 기준
        [법령]_자원순환법 → 자원순환법
    """
    import re
    title = re.sub(r"^\[.*?\]_?", "", raw_title)  # [가이드]_ 등 접두사 제거
    title = title.replace("_", " ")                # 언더스코어 → 공백
    return title.strip() or raw_title


def _build_sources(results: list[dict]) -> list[dict]:
    """검색 결과를 프론트 ChatSource 형식으로 변환한다.

    같은 문서라도 조문이 다르면 별개 근거이므로 (document_id, article) 단위로 묶는다.
    """
    sources: list[dict] = []
    seen: set = set()

    for item in results:
        key = (item.get("document_id"), item.get("label"))
        if key in seen:
            continue
        seen.add(key)

        snippet = " ".join(item["content"].split())  # 줄바꿈·중복 공백 정리
        if len(snippet) > SNIPPET_LENGTH:
            snippet = snippet[:SNIPPET_LENGTH] + "…"

        sources.append(
            {
                "document_id": item.get("document_id"),
                "title": _clean_title(item.get("title", "제목 없음")),
                "snippet": snippet,
            }
        )

    return sources


# ─────────────────── 문서 공급 ───────────────────


def _load_documents(db=None) -> list[dict]:
    """인덱싱 대상 문서를 [{"id","owner_id","title","content","source_type"}, ...] 로 반환."""
    if settings.RAG_SOURCE.lower() == "files":
        return _load_from_files()
    return _load_from_db(db)


def _load_from_db(db=None) -> list[dict]:
    """documents 테이블에서 문서를 읽는다. (RAG_SOURCE=db)

    content_text(평문) → content → summary 순으로 채워진 값을 사용한다.
    프론트가 content 에 에디터 JSON을 저장하므로 평문인 content_text 가 우선이다.
    """
    from app.database import SessionLocal
    from app.models import Document

    own_session = db is None
    session = db or SessionLocal()

    try:
        documents: list[dict] = []

        for row in session.query(Document).all():
            candidates = [
                getattr(row, "content_text", None),
                row.content,
                row.summary,
            ]
            # content 와 content_text 가 같은 값인 경우가 흔하므로 중복을 제거한다.
            # (제거하지 않으면 같은 내용이 두 번 인덱싱되어 검색 결과가 중복된다)
            parts: list[str] = []
            for candidate in candidates:
                if not (isinstance(candidate, str) and candidate.strip()):
                    continue
                if candidate not in parts:
                    parts.append(candidate)

            if not parts:
                continue

            source_type = row.source_type
            # SQLAlchemy Enum 이면 .value, 문자열이면 그대로
            source_type = getattr(source_type, "value", source_type)

            documents.append(
                {
                    "id": row.id,
                    "owner_id": row.owner_id,
                    "title": row.title,
                    # 법령은 조문 단위 분할을 위해 평문 하나만 쓴다
                    "content": parts[0] if source_type == "law" else "\n\n".join(parts),
                    "source_type": source_type,
                    # 파일 경로와 달리 DB 경로는 region 을 채우지 않아
                    # 모든 문서가 "common" 으로 인덱싱되던 버그 수정.
                    # (지역 필터가 무력화되어 타 지역 가이드가 검색되던 문제)
                    "region": _extract_region(row.title or ""),
                }
            )

        if not documents:
            print("[RAG] documents 테이블에 인덱싱할 문서가 없습니다.")

        return documents
    finally:
        if own_session:
            session.close()


def _extract_region(filename: str) -> str:
    """파일명에서 지역 코드를 추출한다.

    예) [가이드]_서울시_... → seoul
        [가이드]_천안시_... → cheonan
        [가이드]_부산남구_... → busan_namgu
        [가이드]_환경부_공통_... → common
    """
    REGION_MAP = {
        "서울": "seoul",
        "천안": "cheonan",
        "부산남구": "busan_namgu",
        "부산": "busan_namgu",
        "공통": "common",
        "환경부": "common",
    }
    for keyword, code in REGION_MAP.items():
        if keyword in filename:
            return code
    return "common"


def _load_from_files() -> list[dict]:
    """data/guide + data/docs 폴더에서 문서를 읽는다. (RAG_SOURCE=files, DB 없이 테스트용)

    파일명이 '[법령]' 으로 시작하면 법령으로 간주해 조문 단위로 청킹한다.
    파일명이 '[가이드]' 로 시작하면 guide 유형으로, 지역명을 추출해 region을 설정한다.
    """
    supported = {".txt", ".md", ".pdf"}
    documents: list[dict] = []

    # data/guide 와 data/docs 두 폴더를 모두 탐색
    search_dirs = [settings.GUIDE_DIR, settings.DOCS_DIR]

    doc_id = 0
    for folder in search_dirs:
        folder.mkdir(parents=True, exist_ok=True)
        for path in sorted(folder.iterdir()):
            if not (path.is_file() and path.suffix.lower() in supported):
                continue

            text = _read_file(path)
            if not text.strip():
                print(f"[RAG] 텍스트를 추출하지 못했습니다: {path.name} (스캔 PDF면 OCR 필요)")
                continue

            doc_id += 1
            stem = path.stem

            # source_type 결정
            if stem.startswith("[법령]"):
                source_type = "law"
            elif stem.startswith("[가이드]"):
                source_type = "guide"
            else:
                source_type = "manual"

            documents.append(
                {
                    "id": doc_id,
                    "owner_id": None,
                    "title": stem,
                    "content": text,
                    "source_type": source_type,
                    "region": _extract_region(stem),
                }
            )

    if not documents:
        print(f"[RAG] 문서를 찾지 못했습니다. 경로: {search_dirs}")
        print(f"      지원 형식: {', '.join(sorted(supported))}")

    return documents


def _read_file(path: Path) -> str:
    """확장자에 맞는 방식으로 텍스트를 추출한다."""
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            print("[RAG] pypdf 가 설치되지 않았습니다.  pip install pypdf")
            return ""
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)

    # Windows 메모장으로 저장한 파일은 cp949 인 경우가 있어 대비
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp949")


# ─────────────────── 답변 생성 ───────────────────


def _generate_answer(question: str, context: str) -> dict:
    """컨텍스트를 근거로 3섹션 답변을 생성한다.

    gemini_service.answer_with_context() 가 있으면 사용하고,
    없으면 검색된 원문을 그대로 보여주는 대체 답변을 반환한다.

    반환: {"guide": str, "law": str, "tip": str}
    """
    try:
        from app.services import gemini_service

        if hasattr(gemini_service, "answer_with_context"):
            return gemini_service.answer_with_context(question, context)
    except ImportError:
        pass

    return {
        "answer": f"[LLM 미연결 상태 · 검색 결과 원문]\n{context}",
        "tip": "",
    }
