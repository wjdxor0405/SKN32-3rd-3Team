# path : scripts/seed_docs.py
"""
[RAG 파트] 공용 문서(법령·가이드)를 documents 테이블에 적재하고 인덱스를 만든다.

    python -m scripts.seed_docs

동작
  1. 시스템 계정 + 로그인용 데모 계정을 만든다
     (documents.owner_id 가 NOT NULL 이고, 프론트에 회원가입 화면이 없기 때문)
  2. data/laws/*   → source_type="law"   (조문 단위로 청킹)
     data/guides/* → source_type="guide" (품목 블록 단위로 청킹)
  3. 벡터 인덱스를 재생성한다

지원 형식: .txt / .md / .pdf (PDF는 머리말·페이지번호를 자동 제거)

문서 제목
  파일 첫 줄이 "[" 로 시작하면 그 줄을 제목으로 쓰고, 아니면 파일명을 쓴다.
  답변에서 "서울시 기준으로는…" 처럼 인용되므로 지역이 드러나는 제목이 좋다.
"""

from __future__ import annotations

import sys

from app.core.config import settings
from app.database import Base, SessionLocal, engine
from app.models import Document, SourceType, User
from app.services import rag_service
from scripts.law_text import check_effective_date, count_articles, read_law_file

SYSTEM_EMAIL = "system@local"
SYSTEM_PASSWORD = "seed-only-change-me"

# 프론트에 회원가입 화면이 없어서 로그인용 데모 계정을 함께 만든다.
# LoginScreen 의 기본 입력값과 같은 이메일이라 비밀번호만 치면 로그인된다.
DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo1234"

SUPPORTED = (".txt", ".md", ".pdf")

# 폴더 안내문 등 자료가 아닌 파일은 제외한다
IGNORED_STEMS = {"readme", "read_me", "notes", "메모"}


# ─────────────────── 계정 ───────────────────


def _get_hasher():
    """팀 구현에 따라 해시 함수 이름이 다를 수 있으므로 모듈에서 찾아 쓴다."""
    from app.core import security

    hasher = getattr(security, "get_password_hash", None) or getattr(
        security, "hash_password", None
    )
    if hasher is None:
        print("[오류] app/core/security.py 에서 비밀번호 해시 함수를 찾지 못했습니다.")
        print("       사용 가능한 이름:", [n for n in dir(security) if "pass" in n.lower()])
        sys.exit(1)
    return hasher


def get_or_create_user(db, email: str, password: str, display_name: str) -> User:
    """계정이 없으면 만들고, 있으면 그대로 돌려준다."""
    user = db.query(User).filter(User.email == email).first()
    if user:
        return user

    user = User(
        email=email,
        hashed_password=_get_hasher()(password),
        display_name=display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    print(f"  계정 생성: {email} (id={user.id})")
    return user


# ─────────────────── 파일 읽기 ───────────────────


def _extract_title(text: str, fallback: str) -> str:
    """첫 줄이 "[서울시] …" 형태면 제목으로 쓰고, 아니면 파일명을 쓴다."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return line if line.startswith("[") and len(line) <= 80 else fallback
    return fallback


def load_folder(folder, source_type: str) -> list[tuple[str, str, str]]:
    """폴더의 문서를 (제목, 본문, source_type) 목록으로 읽는다."""
    folder.mkdir(parents=True, exist_ok=True)
    items: list[tuple[str, str, str]] = []

    for path in sorted(folder.iterdir()):
        if not (path.is_file() and path.suffix.lower() in SUPPORTED):
            continue
        if path.stem.lower() in IGNORED_STEMS or path.stem.startswith("_"):
            continue

        try:
            text = read_law_file(path)
        except Exception as exc:
            print(f"  [건너뜀] {path.name}: {exc}")
            continue

        if not text.strip():
            print(f"  [건너뜀] 내용이 비어 있습니다: {path.name}")
            continue

        title = _extract_title(text, path.stem)

        if source_type == "law":
            articles = count_articles(text)
            if articles == 0:
                print(f"  [경고] {path.name}: 조문(제N조)을 찾지 못했습니다.")
                print("         일반 문자 단위로 분할되어 조문 인용이 어려울 수 있습니다.")
            else:
                print(f"  법령  : {title} — 조문 {articles}개, {len(text):,}자")
            check_effective_date(text, path.name)
        else:
            print(f"  가이드: {title} — {len(text):,}자")

        items.append((title, text, source_type))

    return items


def load_public_docs() -> list[tuple[str, str, str]]:
    """법령·가이드 폴더를 모두 읽는다."""
    return (
        load_folder(settings.LAWS_DIR, "law")
        + load_folder(settings.GUIDES_DIR, "guide")
    )


# ─────────────────── 메인 ───────────────────


def main() -> None:
    Base.metadata.create_all(bind=engine)

    print("[1/3] 공용 문서 읽기")
    docs = load_public_docs()

    if not docs:
        print(f"[중단] {settings.LAWS_DIR} 와 {settings.GUIDES_DIR} 에 파일이 없습니다.")
        print("       txt·md·pdf 를 넣은 뒤 다시 실행하세요.")
        return

    with SessionLocal() as db:
        print("\n[2/3] 계정 확인 및 DB 적재")
        owner = get_or_create_user(db, SYSTEM_EMAIL, SYSTEM_PASSWORD, "시스템")
        get_or_create_user(db, DEMO_EMAIL, DEMO_PASSWORD, "데모 사용자")

        for title, content, source_type in docs:
            doc = (
                db.query(Document)
                .filter(Document.title == title, Document.owner_id == owner.id)
                .first()
            )

            if doc:
                doc.content = content
                doc.content_text = content
                doc.source_type = SourceType(source_type)
                action = "갱신"
            else:
                db.add(
                    Document(
                        owner_id=owner.id,
                        title=title,
                        content=content,
                        content_text=content,
                        source_type=SourceType(source_type),
                    )
                )
                action = "추가"

            print(f"  {action}: [{source_type}] {title}")

        # ── 동기화: 폴더에서 사라진 공용 문서는 DB에서도 지운다 ──
        # 파일명·첫 줄(제목)을 바꾸면 새 문서로 적재되고 옛 레코드가 남아
        # 잘못된 제목으로 인용되는 사고를 막는다.
        # 시스템 계정 소유의 law/guide 문서만 대상이므로 사용자 업로드 문서는 건드리지 않는다.
        seeded_titles = {title for title, _, _ in docs}
        stale = (
            db.query(Document)
            .filter(
                Document.owner_id == owner.id,
                Document.source_type.in_([SourceType("law"), SourceType("guide")]),
                Document.title.notin_(seeded_titles),
            )
            .all()
        )
        for doc in stale:
            print(f"  삭제: [{doc.source_type.value}] {doc.title} (폴더에 없음)")
            db.delete(doc)

        db.commit()

    print("\n[3/3] 인덱스 재생성")
    result = rag_service.rebuild_index()
    print(f"문서 {result['documents']}개 → 청크 {result['indexed_chunks']}개")
    print(f"임베딩 백엔드: {result['embedding_backend']}")

    if result["indexed_chunks"] == 0:
        print("\n[경고] 청크가 0개입니다. .env 의 RAG_SOURCE 가 db 인지 확인하세요.")

    print("\n" + "─" * 50)
    print("로그인 계정 (프론트에 회원가입 화면이 없어 시드로 생성)")
    print(f"  이메일   : {DEMO_EMAIL}")
    print(f"  비밀번호 : {DEMO_PASSWORD}")
    print("─" * 50)


if __name__ == "__main__":
    main()
