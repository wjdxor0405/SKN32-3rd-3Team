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
     # 시행령 별표 8(과태료 부과기준)도 유효 근거 (2026-08-04 gemini eval 반영).
     # 가이드 확장 후 '자주 틀리는 것' 블록이 상위를 차지하는 희석 관찰됨
     # → 법령 근거형 질문의 source_type=law 가중은 RAG 과제로 별도 추진
     {"expect_title": "폐기물관리법", "expect_label": r"제68조|별표\s*8"}),
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
     # 2026-08-03 제주 가이드 추가로 무응답형에서 응답형으로 전환.
     # (이전: expect_empty — 제주가 코퍼스에 없음을 확인하던 자리)
     {"expect_title": "제주", "expect_content": "요일"}),
    ("E2", "음식물처리기 구매 보조금 얼마 받을 수 있어?", None,
     # 2026-08-04 음식물쓰레기 구분기준 가이드(보조금 절 포함) 추가로 응답형 전환.
     # (이전: 무응답형 → 임계값 분리 불가 확인 후 forbid_content 자리표시였음)
     # 금액은 지자체별 상이로 미수록 — 제도 존재와 '지자체 공고 확인' 안내가 잡히는지 검증
     {"expect_title": "음식물", "expect_content": "보조금"}),
    ("E3", "폐기물관리법 시행령 별표 8 내용 알려줘", None,
     # TODO(데이터): 별표 8이 구판 이식·일부 요약 상태라 직접 인용을 막는 취지의 forbid.
     # 별표 8을 현행(제36217호) 기준으로 완전 수록하면
     # {"expect_title": "시행령", "expect_label": r"별표\s*8"} 로 전환할 것 (2026-08-04)
     {"forbid_title": "시행령"}),
    ("E4", "대형폐기물 수수료 얼마야?", None,
     # 2026-08-03 대형폐기물 가이드 추가로 절차 답변이 가능해짐.
     # 금액은 의도적으로 미수록(지자체별 상이) — 절차 근거가 잡히는지 확인
     {"expect_title": "대형폐기물", "expect_content": "지자체"}),
    # F. 버전·인용 정합성
    ("F1", "재활용가능자원 분리수거 지침은 누가 정해?", None,
     {"expect_content": "정할 수 있다", "forbid_content": "정하여야 한다. <개정 2014. 1. 21., 2025. 10. 1., 2026. 2. 19.>"}),
    ("F2", "1회용품 무상제공하면 과태료 얼마야?", None,
     {"expect_label": r"제41조", "max_article_versions": ("제41조(", 1)}),
    ("F3", "배달 용기 분리수거 어떻게 해?", None,
     # 별표 1(합성수지 용기·트레이류) 또는 가이드의 용기 배출 요령 중 하나면 정답
     {"expect_content": "내용물"}),
    # G. 특수 배출 가이드형 (2026-08-03 신규 3종 대상)
    ("G1", "장롱 버리려면 어떻게 해야 해?", None,
     # 품목 블록 청킹 특성상 어느 블록이 잡히느냐에 따라 본문 조건이 흔들림 → 제목만 검증 (2026-08-04)
     {"expect_title": "대형폐기물"}),
    ("G2", "냉장고 버릴 때 돈 내야 해?", None,
     {"expect_title": "무상방문수거", "expect_content": "1599-0903"}),
    ("G3", "선풍기 하나만 버리고 싶은데 무상수거 되나?", None,
     {"expect_title": "무상방문수거", "expect_content": "소형가전"}),
    ("G4", "먹다 남은 약은 어디에 버려?", None,
     {"expect_title": "유해폐기물", "expect_content": "폐의약품"}),
    ("G5", "물약도 우체통에 넣어도 돼?", None,
     {"expect_title": "유해폐기물", "expect_content": "우체통에는 넣지 말"}),
    ("G8", "닭뼈는 음식물쓰레기야?", None,
     {"expect_title": "음식물", "expect_content": "일반쓰레기"}),
    ("G9", "커피 찌꺼기는 음식물이야 일반쓰레기야?", None,
     # 지자체별로 갈리는 품목 — '거주지 확인' 안내가 근거로 잡히는지 검증
     {"expect_title": "음식물", "expect_content": "지자체"}),
    # H. 홈페이지 확장 카테고리형 — 일회용품·에너지 (2026-08-04 신규 2종 대상)
    ("H1", "카페 일회용컵 규제 지금 어떻게 돼?", None,
     {"expect_title": "일회용품", "expect_content": "플라스틱컵"}),
    ("H2", "1회용컵 보증금제가 뭐야?", None,
     # 2025.12 전국 의무화 철회·지자체 자율 전환, 제주 유지 — 현행 상태가 잡히는지 검증
     {"expect_title": "일회용품", "expect_content": "제주"}),
    ("H3", "생분해 플라스틱은 분리배출하면 돼?", None,
     {"expect_title": "일회용품", "expect_content": "종량제"}),
    ("H4", "여름에 에어컨 적정 온도가 몇 도야?", None,
     {"expect_title": "에너지", "expect_content": "26"}),
    ("H5", "대기전력 차단하면 얼마나 절약돼?", None,
     {"expect_title": "에너지", "expect_content": "대기전력"}),
    ("H6", "탄소중립 포인트제가 뭐야?", None,
     {"expect_title": "탄소중립", "expect_content": "녹색생활"}),
    ("H7", "탄소 발자국이 뭐야?", None,
     {"expect_title": "탄소중립", "expect_content": "온실가스"}),
    ("H8", "그린워싱이 뭐야? 어떻게 구별해?", None,
     {"expect_title": "친환경", "expect_content": "인증"}),
    ("H9", "무라벨 제품이 왜 좋은 거야?", None,
     {"expect_title": "친환경", "expect_content": "재활용"}),
    ("H10", "친환경 세제 추천해줘", None,
     # 특정 상표가 아닌 '환경표지 마크 확인법'으로 답하는 설계 검증
     {"expect_title": "친환경", "expect_content": "환경표지"}),
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
        # 주의: 공백 제거 시 "제41조 (이어서)" 도 "제41조(" 로 시작하므로
        #       "(이어서" 는 명시적으로 제외한다.
        header, limit = mc
        header_flat = header.replace(" ", "")
        n = 0
        for r in results:
            head = r.get("content", "").strip().replace(" ", "")[:30]
            if head.startswith(header_flat) and not head.startswith(header_flat + "이어서"):
                n += 1
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
