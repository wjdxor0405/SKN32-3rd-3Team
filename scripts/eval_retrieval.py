"""골든 질문 세트 — 검색 단계 전용 평가 (LLM 호출 없음)

실행:  python -m scripts.eval_retrieval          # 전체 실행
       python -m scripts.eval_retrieval --show   # FAIL 문항의 검색 결과 상세 출력

원리:
  RAG 파이프라인은 검색 → 컨텍스트 → LLM 순서다. 골든 세트가 검증하려는
  "올바른 문서·조문이 근거로 잡히는가"는 검색 단계에서 결정되므로,
  rag_service.search() 만 호출해 결과의 (제목, 라벨, 본문)을 기대값과 대조한다.
  Gemini 생성 API를 한 번도 부르지 않는다.
  (EMBEDDING_BACKEND=gemini 면 질문 임베딩 호출은 발생 — local 백엔드면 그것도 0회)

판정 규칙:
  expect_title    : 검색 결과 중 제목에 이 문자열이 포함된 청크가 있어야 함
  expect_label    : 그 청크(또는 아무 청크)의 라벨이 이 정규식과 일치해야 함
  expect_content  : 검색 결과 본문 어딘가에 이 문자열이 있어야 함
  expect_empty    : 검색 결과가 비어야 함 (무응답형 — 임계값 검증)
  forbid_content  : 이 문자열이 나오면 FAIL (버전 혼입 검사)
"""
from __future__ import annotations

import re
import sys

from app.services import rag_service

# (id, 질문, region, 기대조건 dict)
GOLDEN: list[tuple[str, str, str | None, dict]] = [
    # A. 법령 근거형
    ("A1", "쓰레기를 아무 데나 버리면 어떤 법에 걸려?", None,
     {"expect_title": "폐기물관리법", "expect_label": r"제8조"}),
    ("A2", "무단투기하면 과태료 얼마까지 나와?", None,
     {"expect_title": "폐기물관리법", "expect_label": r"제68조"}),
    ("A3", "분리배출 표시(재질 마크)는 무슨 법에 근거해?", None,
     {"expect_title": "자원의 절약", "expect_label": r"제14조"}),
    ("A4", "카페에서 1회용컵 공짜로 주면 불법이야?", None,
     {"expect_title": "자원의 절약", "expect_label": r"제10조"}),
    ("A5", "소주병 반납하면 돈 돌려받는 근거가 뭐야?", None,
     {"expect_title": "자원의 절약", "expect_label": r"제15조의2"}),
    ("A6", "지자체는 재활용품 정기수거일을 지정해야 해?", None,
     {"expect_title": "분리수거", "expect_label": r"제5조"}),
    ("A7", "아파트 관리사무소 같은 폐기물배출자는 분리보관 의무가 있어?", None,
     {"expect_label": r"제12조의3|제9조"}),
    # B. 품목형 (별표 1 라벨 패치 검증)
    ("B1", "투명 페트병은 어떻게 버려?", None,
     {"expect_content": "라벨", "expect_label": r"별표\s*1|페트"}),
    ("B2", "깨진 유리컵은 재활용 돼?", None,
     {"expect_content": "종량제"}),
    ("B3", "부탄가스통은 어떻게 버려?", None,
     {"expect_content": "노즐"}),
    ("B4", "컵라면 용기 스티로폼에 국물 자국 있는데 재활용 돼?", None,
     {"expect_content": "이물질"}),
    ("B5", "다 쓴 건전지는 어디에 버려?", None,
     {"expect_content": "전지"}),
    ("B6", "헌옷 버릴 때 이불도 같이 의류수거함에 넣어도 돼?", None,
     {"expect_content": "이불"}),
    # C. 지역형 (region 파라미터 = 실제 서비스와 동일 경로)
    ("C1", "투명 페트병 언제 배출해?", "cheonan",
     {"expect_title": "천안", "expect_content": "목요일"}),
    ("C2", "재활용품 배출 요일 알려줘", "busan_namgu",
     {"expect_title": "부산", "expect_content": "화요일"}),
    ("C3", "유모차는 어떻게 버려?", "seoul",
     {"expect_content": "대형폐기물"}),
    ("C4", "분리배출 기준은 자치구마다 달라?", "seoul",
     {"expect_title": "서울"}),
    # D. 지역 비교형 — region 필터 없이 두 지역 문서가 함께 잡히는지
    ("D1", "서울이랑 천안이랑 투명 페트병 버리는 방법 차이 있어?", None,
     {"expect_titles_all": ["서울", "천안"]}),
    ("D2", "부산 남구랑 서울 중에 스티로폼 배출 방법이 다른 점은?", None,
     {"expect_titles_all": ["부산", "서울"]}),
    # E. 무응답형 — 임계값(RAG_MIN_SCORE)이 걸러내는지
    ("E1", "제주도 분리배출 요일 알려줘", "jeju",
     {"expect_empty": True}),
    ("E2", "음식물처리기 구매 보조금 얼마 받을 수 있어?", None,
     {"expect_empty": True}),
    ("E3", "폐기물관리법 시행령 별표 8 내용 알려줘", None,
     {"forbid_title": "시행령"}),
    ("E4", "대형폐기물 수수료 얼마야?", None,
     {"forbid_content": "수수료는 0"}),  # 금액이 코퍼스에 없음을 확인하는 자리표시
    # F. 버전·인용 정합성
    ("F1", "재활용가능자원 분리수거 지침은 누가 정해?", None,
     {"expect_content": "정할 수 있다", "forbid_content": "정하여야 한다. <개정 2014. 1. 21., 2025. 10. 1., 2026. 2. 19.>"}),
    ("F2", "1회용품 무상제공하면 과태료 얼마야?", None,
     {"expect_label": r"제41조", "max_article_versions": ("제41조(", 1)}),
    ("F3", "배달 용기 분리수거 어떻게 해?", None,
     # 별표 1(합성수지 용기·트레이류) 또는 가이드의 용기 배출 요령 중 하나면 정답
     {"expect_content": "내용물"}),
]


def _label_of(chunk: dict) -> str:
    """청크의 라벨. 인덱스 메타에 없으면 본문 앞머리에서 추출."""
    label = chunk.get("label") or ""
    if not label:
        head = chunk.get("content", "").strip()[:30]
        m = re.match(r"(제\s*\d+\s*조(?:의\s*\d+)?|별\s*표\s*\d+)", head)
        label = m.group(1) if m else ""
    return label.replace(" ", "")


def judge(expect: dict, results: list[dict]) -> tuple[bool, str]:
    titles = [r.get("title", "") for r in results]
    labels = [_label_of(r) for r in results]
    all_text = "\n".join(r.get("content", "") for r in results)
    # PDF 추출 데이터에 단어 중간 공백이 남아 있어("정 할 수 있다")
    # 본문 매칭은 공백을 전부 제거한 문자열끼리 비교한다.
    flat = re.sub(r"\s+", "", all_text)

    def in_text(needle: str) -> bool:
        return re.sub(r"\s+", "", needle) in flat

    if expect.get("expect_empty"):
        return (not results,
                "검색 결과 없음(정상)" if not results
                else f"결과 {len(results)}건 반환 — 임계값 점검 필요: {titles[:2]}")

    if not results:
        return False, "검색 결과 없음"

    reasons = []
    ok = True

    if t := expect.get("expect_title"):
        hit = any(t in x for x in titles)
        ok &= hit
        reasons.append(f"제목'{t}' {'○' if hit else '× ' + str(titles[:3])}")

    if ts := expect.get("expect_titles_all"):
        for t in ts:
            hit = any(t in x for x in titles)
            ok &= hit
            reasons.append(f"제목'{t}' {'○' if hit else '×'}")

    if lb := expect.get("expect_label"):
        hit = any(re.search(lb, x) for x in labels)
        ok &= hit
        reasons.append(f"라벨/{lb}/ {'○' if hit else '× ' + str(labels)}")

    if c := expect.get("expect_content"):
        hit = in_text(c)
        ok &= hit
        reasons.append(f"본문'{c}' {'○' if hit else '×'}")

    if fc := expect.get("forbid_content"):
        bad = in_text(fc)
        ok &= not bad
        if bad:
            reasons.append(f"금지 본문'{fc[:20]}…' 발견 ×")

    if ft := expect.get("forbid_title"):
        bad = any(ft in x for x in titles)
        ok &= not bad
        if bad:
            reasons.append(f"금지 제목'{ft}' 발견 ×")

    if mc := expect.get("max_article_versions"):
        # 같은 조문 "헤더"로 시작하는 청크가 2개면 현행판+미래판 중복 혼입.
        # 긴 조문의 하위분할("제41조 (이어서)")은 정상이므로 세지 않는다.
        header, limit = mc
        n = sum(
            1 for r in results
            if r.get("content", "").strip().replace(" ", "").startswith(
                header.replace(" ", "")
            )
        )
        hit = n <= limit
        ok &= hit
        reasons.append(f"조문헤더'{header}' {n}개{'○' if hit else '(중복판!)×'}")

    return ok, ", ".join(reasons)


def main() -> None:
    show = "--show" in sys.argv
    results_table = []
    pass_top_scores: list[tuple[str, float]] = []   # 정답이 잡혀야 하는 문항의 1위 점수
    empty_top_scores: list[tuple[str, float]] = []  # 무응답이어야 하는 문항의 1위 점수

    for qid, question, region, expect in GOLDEN:
        chunks = rag_service.search(question, region=region)
        ok, detail = judge(expect, chunks)
        results_table.append((qid, ok, question, detail))

        top = chunks[0].get("score", 0.0) if chunks else 0.0
        if expect.get("expect_empty"):
            empty_top_scores.append((qid, top))
        elif not any(k.startswith("forbid") for k in expect) or len(expect) > 1:
            pass_top_scores.append((qid, top))

        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {qid} {question}")
        print(f"       {detail}")
        if show and not ok:
            for r in chunks:
                print(f"       · score={r.get('score', 0):.3f} "
                      f"[{r.get('title', '')}] {_label_of(r)} "
                      f"{r.get('content', '')[:60].strip()}…")

    passed = sum(1 for _, ok, _, _ in results_table if ok)
    print(f"\n결과: {passed}/{len(results_table)} PASS")

    # ── 임계값 캘리브레이션 도우미 ──
    # RAG_MIN_SCORE 를 "정답 문항 최저 1위 점수"와 "무응답 문항 최고 1위 점수"
    # 사이에 두면 환각 방지와 검색 recall 을 동시에 만족한다.
    if pass_top_scores and empty_top_scores:
        lo_q, lo = min(pass_top_scores, key=lambda x: x[1])
        hi_q, hi = max(empty_top_scores, key=lambda x: x[1])
        print("\n[임계값 캘리브레이션]")
        print(f"  정답 문항 최저 1위 점수  : {lo:.3f} ({lo_q}) — 임계값이 이보다 높으면 정답을 놓침")
        print(f"  무응답 문항 최고 1위 점수: {hi:.3f} ({hi_q}) — 임계값이 이보다 낮으면 환각 위험")
        if hi < lo:
            mid = (hi + lo) / 2
            print(f"  → 권장 RAG_MIN_SCORE: {mid:.2f} (분리 구간 {hi:.3f}~{lo:.3f})")
        else:
            print("  → 분포가 겹칩니다. 임계값만으로는 분리 불가 —")
            print("     region 필터 정상화 + LLM 프롬프트의 '근거 없으면 모른다' 2차 방어 병행 필요")

    print("\n주의: 이 평가는 검색 단계만 검증한다. 답변 문장 품질·G(경계형) 문항은")
    print("      소수의 실제 LLM 호출로 별도 확인할 것.")


if __name__ == "__main__":
    main()
