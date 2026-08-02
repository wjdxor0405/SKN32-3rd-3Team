# path : scripts/law_text.py
"""
[RAG 파트] 법령 파일에서 텍스트를 뽑아 정제한다.

PDF 법령은 그대로 쓰면 문제가 많다.
  - 페이지마다 "법제처 / 국가법령정보센터" 같은 머리말·꼬리말이 반복된다
  - "- 1 -" 같은 페이지 번호가 본문 사이에 끼어든다
  - 한 문장이 줄바꿈으로 여러 줄에 쪼개져 있다

이 노이즈를 그대로 인덱싱하면 검색 결과에 계속 섞여 나오고
LLM 컨텍스트도 낭비된다. 그래서 적재 전에 걸러낸다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# 페이지 번호로 보이는 줄: "- 1 -", "1", "1/23", "Page 1", "1 쪽"
PAGE_NUMBER = re.compile(
    r"^\s*(?:-\s*\d+\s*-|\d+\s*/\s*\d+|\d{1,4}|page\s*\d+|\d+\s*쪽)\s*$",
    re.IGNORECASE,
)

# 줄이 새 문단으로 시작하는지 판단 — 이런 줄 앞에서는 줄바꿈을 유지한다
NEW_BLOCK = re.compile(
    r"^\s*(?:"
    r"제\s*\d+\s*조"          # 제15조
    r"|제\s*\d+\s*장"          # 제2장
    r"|제\s*\d+\s*절"
    r"|[①-⑳]"                # 항 기호
    r"|\d+\.\s"               # 1. 
    r"|[가-힣]\.\s"           # 가. 
    r"|부\s*칙"
    r"|\[.+\]\s*$"          # [종이류] 같은 품목 머리글
    r"|=+\s*.+\s*=+\s*$"    # === 품목별 분리배출 요령 ===
    r")"
)


def read_law_file(path: Path) -> str:
    """확장자에 맞게 텍스트를 뽑고 정제해서 돌려준다."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return clean_law_text(_extract_pdf_pages(path))

    # txt·md 는 줄 구조가 이미 온전하므로 정제하지 않는다.
    # (PDF용 줄 잇기를 적용하면 가이드 문서의 "[종이류]" 같은 블록 구분이 사라진다)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp949")  # 메모장 저장 대비


def _extract_pdf_pages(path: Path) -> list[str]:
    """PDF를 페이지별 텍스트 목록으로 뽑는다."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf 가 필요합니다.  pip install pypdf")

    pages = [page.extract_text() or "" for page in PdfReader(str(path)).pages]

    if not any(p.strip() for p in pages):
        raise RuntimeError(
            "텍스트를 추출하지 못했습니다. 스캔 이미지 PDF일 수 있습니다 (OCR 필요)."
        )

    return pages


def clean_law_text(pages: list[str]) -> str:
    """페이지 목록을 하나의 정제된 본문으로 합친다."""
    repeated = _find_repeated_lines(pages)

    kept: list[str] = []
    for page in pages:
        for line in page.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped in repeated:      # 머리말·꼬리말
                continue
            if PAGE_NUMBER.match(stripped):  # 페이지 번호
                continue
            kept.append(stripped)

    return _join_wrapped_lines(kept)


def _find_repeated_lines(pages: list[str], ratio: float = 0.8) -> set[str]:
    """여러 페이지에 반복 등장하는 짧은 줄 = 머리말·꼬리말로 본다.

    본문이 우연히 반복되는 것을 지우지 않도록 40자 이하 줄만 대상으로 한다.
    또한 전체 페이지의 80% 이상에 나타나야 머리말로 판정한다.
    (본문 문장이 두어 번 반복된다고 지워지면 안 되기 때문)
    """
    if len(pages) < 2:
        return set()

    counter: Counter[str] = Counter()
    for page in pages:
        # 같은 페이지 안의 중복은 1회로 계산
        lines = {ln.strip() for ln in page.splitlines() if ln.strip()}
        counter.update(lines)

    threshold = len(pages) * ratio
    return {
        line
        for line, count in counter.items()
        if count >= threshold and len(line) <= 40
    }


def _join_wrapped_lines(lines: list[str]) -> str:
    """PDF에서 줄바꿈으로 쪼개진 문장을 다시 잇는다.

    조문·항·호로 시작하는 줄 앞에서만 문단을 나누고,
    나머지는 앞 줄에 이어 붙인다. 이렇게 해야 조문 단위 청킹이 깔끔해진다.
    """
    blocks: list[str] = []

    for line in lines:
        if not blocks or NEW_BLOCK.match(line):
            blocks.append(line)
        else:
            blocks[-1] = f"{blocks[-1]} {line}"

    return "\n\n".join(blocks)


def count_articles(text: str) -> int:
    """본문에서 인식된 조문 수. 추출이 제대로 됐는지 확인하는 지표."""
    return len(re.findall(r"제\s*\d+\s*조(?:의\s*\d+)?\s*[(（]", text))


# ─────────────────── 시행일 검사 ───────────────────

# 법령 헤더의 "[시행 2026. 5. 12.]" 표기
EFFECTIVE_DATE = re.compile(r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*\]")

# 시행예정 중복판 마커: "[시행일: 2027. 1. 8.] 제9조"
FUTURE_VERSION_MARKER = re.compile(r"^\[시행일:\s*[\d\.\s]+\]\s*제\d+조", re.MULTILINE)


def check_effective_date(text: str, fname: str) -> None:
    """법령 헤더의 시행일을 오늘과 비교해 시행예정판·구버전 여부를 경고한다.

    law.go.kr 에서 시행예정판을 복사해 오는 실수를 적재 로그에서 잡기 위한 것.
    자동으로 고치지는 않는다 — 조문 0개 경고와 같은 철학.
    """
    from datetime import date

    m = EFFECTIVE_DATE.search(text[:500])  # 헤더 부근만 검사
    if not m:
        print(f"  [참고] {fname}: 시행일 표기([시행 YYYY. M. D.])가 없습니다. 버전 확인 권장")
        return

    eff = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    today = date.today()

    if eff > today:
        print(f"  [경고] {fname}: 시행일 {eff} — 아직 시행 전(시행예정판)입니다.")
        print("         law.go.kr 에서 '현행' 표시가 붙은 판을 다시 받으세요.")
    elif (today - eff).days > 365 * 3:
        print(f"  [참고] {fname}: 시행일 {eff} — 3년 이상 경과. 개정 여부 확인 권장")

    if FUTURE_VERSION_MARKER.search(text):
        print(f"  [경고] {fname}: 시행예정 중복 조문([시행일: …] 제N조)이 남아 있습니다.")
        print("         같은 조문이 두 번 인덱싱되어 인용이 꼬일 수 있으니 미래판 블록을 제거하세요.")
