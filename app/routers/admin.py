# path : app/routers/admin.py
"""
관리자 대시보드 API.

  GET  /api/admin/stats          ← 요약 통계 (총 질문, 오늘, 활성 사용자, 답변 성공률)
  GET  /api/admin/top-questions  ← 인기 질문 TOP N
  GET  /api/admin/region-stats   ← 지역별 질문 분포
  GET  /api/admin/daily-trend    ← 최근 7일 일별 질문 수
  GET  /api/admin/documents      ← 인덱싱된 문서 목록
  POST /api/admin/upload         ← 새 문서 업로드 + 자동 인덱싱
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import func as sql_func, distinct, case
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models import User, ChatLog
from app.routers.api import get_current_user
from app.services import vector_store_service, rag_service

router = APIRouter()


def _require_admin(user: User = Depends(get_current_user)) -> User:
    """이메일에 'admin'이 포함된 사용자만 접근 가능."""
    if "admin" not in user.email.lower():
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


# ─────────────────── 요약 통계 ───────────────────


@router.get("/admin/stats")
def get_stats(user: User = Depends(_require_admin), db: Session = Depends(get_db)):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    prev_week = week_ago - timedelta(days=7)

    total = db.query(sql_func.count(ChatLog.id)).scalar() or 0
    today_count = db.query(sql_func.count(ChatLog.id)).filter(ChatLog.created_at >= today).scalar() or 0
    yesterday = today - timedelta(days=1)
    yesterday_count = db.query(sql_func.count(ChatLog.id)).filter(
        ChatLog.created_at >= yesterday, ChatLog.created_at < today
    ).scalar() or 0

    # 이번 주 활성 사용자
    active_users = db.query(sql_func.count(distinct(ChatLog.user_id))).filter(
        ChatLog.created_at >= week_ago
    ).scalar() or 0

    # 답변 성공률
    answered = db.query(sql_func.count(ChatLog.id)).filter(ChatLog.has_answer == True).scalar() or 0
    success_rate = round(answered / total * 100) if total > 0 else 0

    # 지난주 대비 증감
    this_week = db.query(sql_func.count(ChatLog.id)).filter(ChatLog.created_at >= week_ago).scalar() or 0
    last_week = db.query(sql_func.count(ChatLog.id)).filter(
        ChatLog.created_at >= prev_week, ChatLog.created_at < week_ago
    ).scalar() or 0
    week_change = round((this_week - last_week) / last_week * 100) if last_week > 0 else 0

    return {
        "total": total,
        "today": today_count,
        "yesterday": yesterday_count,
        "today_diff": today_count - yesterday_count,
        "active_users": active_users,
        "success_rate": success_rate,
        "week_change": week_change,
    }


# ─────────────────── 인기 질문 ───────────────────


@router.get("/admin/top-questions")
def get_top_questions(
    limit: int = 5,
    user: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ChatLog.question, sql_func.count(ChatLog.id).label("cnt"))
        .group_by(ChatLog.question)
        .order_by(sql_func.count(ChatLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"question": r.question, "count": r.cnt} for r in rows]


# ─────────────────── 지역별 분포 ───────────────────


REGION_LABELS = {
    "seoul": "서울",
    "cheonan": "천안",
    "busan_namgu": "부산 남구",
    "sejong": "세종",
    "incheon_michuhol": "인천 미추홀구",
    "jeju": "제주",
}


@router.get("/admin/region-stats")
def get_region_stats(user: User = Depends(_require_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(ChatLog.region, sql_func.count(ChatLog.id).label("cnt"))
        .group_by(ChatLog.region)
        .order_by(sql_func.count(ChatLog.id).desc())
        .all()
    )
    return [
        {"region": r.region, "label": REGION_LABELS.get(r.region, r.region), "count": r.cnt}
        for r in rows
    ]


# ─────────────────── 일별 추이 ───────────────────


@router.get("/admin/daily-trend")
def get_daily_trend(days: int = 7, user: User = Depends(_require_admin), db: Session = Depends(get_db)):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for i in range(days - 1, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        count = db.query(sql_func.count(ChatLog.id)).filter(
            ChatLog.created_at >= day_start, ChatLog.created_at < day_end
        ).scalar() or 0
        result.append({
            "date": day_start.strftime("%m/%d"),
            "day": ["월", "화", "수", "목", "금", "토", "일"][day_start.weekday()],
            "count": count,
        })
    return result


# ─────────────────── 문서 관리 ───────────────────


@router.get("/admin/documents")
def get_documents(user: User = Depends(_require_admin)):
    """인덱싱된 문서 목록과 청크 수를 반환한다."""
    import json
    from app.core.config import settings

    meta_path = settings.INDEX_DIR / "chunks.json"
    if not meta_path.exists():
        return {"index_exists": False, "documents": [], "total_chunks": 0}

    chunks = json.loads(meta_path.read_text(encoding="utf-8"))

    # 문서별 청크 수 집계
    doc_map: dict[str, dict] = {}
    for chunk in chunks:
        title = chunk.get("title", "제목 없음")
        if title not in doc_map:
            # 파일명 정리
            clean = title
            import re
            clean = re.sub(r"^\[.*?\]_?", "", clean).replace("_", " ").strip() or title
            doc_map[title] = {
                "title": clean,
                "source_type": chunk.get("source_type", "manual"),
                "region": chunk.get("region", "common"),
                "chunk_count": 0,
            }
        doc_map[title]["chunk_count"] += 1

    region_labels = {
        "seoul": "서울", "cheonan": "천안", "busan_namgu": "부산 남구", "common": "공통",
    }
    docs = list(doc_map.values())
    for d in docs:
        d["region_label"] = region_labels.get(d["region"], d["region"])
        d["type_label"] = "가이드" if d["source_type"] == "guide" else "법령" if d["source_type"] == "law" else "기타"

    return {
        "index_exists": True,
        "documents": docs,
        "total_chunks": len(chunks),
    }


# ─────────────────── 문서 업로드 ───────────────────

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}


@router.post("/admin/upload")
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(_require_admin),
):
    """새 문서를 업로드하고 인덱스를 재빌드한다."""
    # 확장자 검증
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"허용되지 않는 파일 형식입니다. ({', '.join(ALLOWED_EXTENSIONS)}만 가능)",
        )

    # data/guide 폴더에 저장
    save_dir = settings.GUIDE_DIR
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / file.filename

    # 동일 파일명이 있으면 덮어쓰기
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 인덱스 자동 재빌드
    try:
        result = rag_service.rebuild_index()
    except Exception as exc:
        return {
            "uploaded": True,
            "filename": file.filename,
            "rebuild_error": str(exc),
        }

    return {
        "uploaded": True,
        "filename": file.filename,
        "indexed_chunks": result.get("indexed_chunks", 0),
    }
