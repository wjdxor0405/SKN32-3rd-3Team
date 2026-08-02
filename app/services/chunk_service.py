# path : app/services/chunk_service.py
"""
[RAG 파트] 문서 텍스트를 검색 단위 청크로 분할합니다.

문서 성격에 따라 분할 방식을 달리한다.
  - 법령(law)   : **조문 단위** (제N조)
  - 가이드(guide): **섹션·품목 단위** ([종이류], [비닐류] …)
  - 그 외        : 문자 수 슬라이딩 윈도우

법령을 조문 단위로 자르는 이유:
  700자에서 기계적으로 자르면 조문 경계가 뭉개져서
  "자원순환법 제15조에 따라" 같은 정확한 인용이 불가능해진다.
  조문 하나 = 청크 하나로 만들면 검색 결과가 곧 인용 단위가 된다.
"""

from __future__ import annotations

import re

from app.core.config import settings

# "제15조(정의)", "제15조 (정의)", "제15조의2(...)" 앞에서 자르기 위한 패턴
# 별표(예: "별표 1. …")도 독립 청크가 되도록 경계에 포함한다.
# 그러지 않으면 별표 전체가 마지막 조문(예: 제16조) 라벨로 인덱싱되어
# LLM이 별표 내용을 엉뚱한 조문으로 인용하게 된다.
ARTICLE_SPLIT = re.compile(
    r"(?=제\s*\d+\s*조(?:의\s*\d+)?\s*[(（])"
    r"|(?=\n\s*별\s*표\s*\d+)"
)

# 청크 앞머리에서 조문 번호만 뽑아내는 패턴 → "제15조", "제15조의2"
ARTICLE_LABEL = re.compile(r"^제\s*(\d+)\s*조(?:의\s*(\d+))?")

# 청크 앞머리의 별표 번호 → "별표 1"
ANNEX_LABEL = re.compile(r"^별\s*표\s*(\d+)")

# 가이드 문서의 큰 구획: "=== 품목별 분리배출 요령 ==="
SECTION_HEADER = re.compile(r"^\s*=+\s*(.+?)\s*=+\s*$")

# 가이드 문서의 품목 블록: "[종이류]", "[음식물쓰레기로 배출하는 것 (○)]"
ITEM_HEADER = re.compile(r"^\s*\[(.+)\]\s*$")


def split_text(
    text: str,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """긴 텍스트를 chunk_size 문자 단위로, chunk_overlap 만큼 겹치게 자릅니다.

    겹침을 두는 이유:
      청크 경계에서 문장이 잘리면 검색 시 문맥이 끊기므로,
      인접 청크가 일부 내용을 공유하게 해 경계 손실을 줄인다.
    """
    chunk_size = chunk_size or settings.CHUNK_SIZE
    chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

    if not text or not text.strip():
        return []

    step = max(1, chunk_size - chunk_overlap)

    chunks: list[str] = []
    for start in range(0, len(text), step):
        end = start + chunk_size
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break

    return chunks


def split_by_article(text: str) -> list[str]:
    """법령 텍스트를 조문 단위로 분할합니다.

    조문이 하나도 인식되지 않으면(형식이 다른 문서면) 일반 분할로 되돌아간다.
    조문 하나가 지나치게 길면 그 안에서 다시 자르되, 앞머리에 조문 번호를 붙여
    잘린 뒷부분도 어느 조문인지 알 수 있게 한다.
    """
    if not text or not text.strip():
        return []

    parts = [p.strip() for p in ARTICLE_SPLIT.split(text) if p.strip()]

    # 조문 패턴을 못 찾았으면 일반 분할로 대체
    if len(parts) < 2:
        return split_text(text)

    limit = settings.CHUNK_SIZE * 2
    chunks: list[str] = []

    for part in parts:
        if len(part) <= limit:
            chunks.append(part)
            continue

        # 긴 조문은 쪼개되 조문 번호를 각 조각에 유지
        label = extract_article_label(part) or ""
        for i, piece in enumerate(split_text(part)):
            chunks.append(piece if i == 0 or not label else f"{label} (이어서)\n{piece}")

    return chunks


def extract_article_label(text: str) -> str | None:
    """청크 앞머리에서 조문/별표 번호를 뽑아냅니다. 예) "제15조", "제15조의2", "별표 1" """
    stripped = text.strip()

    annex = ANNEX_LABEL.match(stripped)
    if annex:
        return f"별표 {annex.group(1)}"

    match = ARTICLE_LABEL.match(stripped)
    if not match:
        return None
    if match.group(2):
        return f"제{match.group(1)}조의{match.group(2)}"
    return f"제{match.group(1)}조"


def split_by_section(text: str) -> list[dict]:
    """가이드 문서를 섹션·품목 단위로 분할한다.

    이 문서들은 `=== 큰 구획 ===` 아래에 `[품목]` 블록이 나열된 구조다.
    700자로 기계적으로 자르면 "[종이류]" 설명이 중간에 끊겨
    "종이컵은 어디에 버려요?" 같은 질문에 반쪽짜리 근거만 잡힌다.

    품목 블록 하나 = 청크 하나로 만들되, 어느 구획에 속했는지 알 수 있도록
    섹션 제목을 청크 앞에 붙인다. (검색·인용 모두에 도움이 된다)

    반환: [{"content": 본문, "label": "품목별 분리배출 요령 > 종이류"}, ...]
    """
    if not text or not text.strip():
        return []

    section = ""          # 현재 === 구획 ===
    item = ""             # 현재 [품목]
    buffer: list[str] = []
    chunks: list[dict] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        label = " > ".join(p for p in (section, item) if p)
        header = f"[{label}]" if label else ""
        chunks.append(
            {
                "content": f"{header}\n{body}".strip(),
                "label": label or None,
            }
        )

    for line in text.splitlines():
        section_match = SECTION_HEADER.match(line)
        if section_match:
            flush()
            section = section_match.group(1)
            item = ""
            continue

        item_match = ITEM_HEADER.match(line)
        if item_match:
            flush()
            item = item_match.group(1)
            continue

        buffer.append(line)

    flush()

    # 구획·품목 표시가 없는 문서면 일반 분할로 되돌린다
    if len(chunks) < 2:
        return [{"content": c, "label": None} for c in split_text(text)]

    # 지나치게 긴 블록은 한 번 더 자르되 라벨을 유지한다
    limit = settings.CHUNK_SIZE * 2
    result: list[dict] = []
    for chunk in chunks:
        if len(chunk["content"]) <= limit:
            result.append(chunk)
            continue
        for piece in split_text(chunk["content"]):
            result.append({"content": piece, "label": chunk["label"]})

    return result


def build_chunks(documents: list[dict]) -> list[dict]:
    """문서 목록 전체를 청크 목록으로 변환합니다.

    입력:  [{"id", "owner_id", "title", "content", "source_type"}, ...]
    출력:  [{"content", "document_id", "owner_id", "title",
              "source_type", "chunk_index", "article"}, ...]

    - owner_id   : 검색 시 본인 문서만 돌려주기 위한 필터 키
    - source_type: 공용 문서 여부 판별 + 답변에서 근거 종류 구분
    - label      : 인용 표시용. 법령은 "제15조", 가이드는 "품목별 요령 > 종이류"
    """
    chunks: list[dict] = []

    for doc in documents:
        source_type = doc.get("source_type") or "manual"
        content = doc.get("content", "")

        if source_type == "law":
            pieces = [
                {"content": c, "label": extract_article_label(c)}
                for c in split_by_article(content)
            ]
        elif source_type == "guide":
            pieces = split_by_section(content)
        else:
            pieces = [{"content": c, "label": None} for c in split_text(content)]

        for chunk_index, piece in enumerate(pieces):
            chunks.append(
                {
                    "content": piece["content"],
                    "document_id": doc.get("id"),
                    "owner_id": doc.get("owner_id"),
                    "title": doc.get("title", "제목 없음"),
                    "source_type": source_type,
                    "region": doc.get("region", "common"),
                    "chunk_index": chunk_index,
                    # 법령이면 "제15조", 가이드면 "품목별 분리배출 요령 > 종이류"
                    "label": piece["label"],
                }
            )

    return chunks
