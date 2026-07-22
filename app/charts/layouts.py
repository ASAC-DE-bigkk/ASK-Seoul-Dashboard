"""사용자별 Charts Studio 레이아웃 RDB 저장소.

첫 접근 시 커밋된 layouts.seed.json을 사용자 전용 행으로 복제한다. 이후 모든 CRUD는
사용자 ID를 조건으로 실행하므로 다른 사용자의 레이아웃을 추측하거나 덮어쓸 수 없다.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import DashboardLayout


SEED_PATH = Path(__file__).parent / "data" / "layouts.seed.json"


class NotFound(KeyError):
    pass


class LimitExceeded(ValueError):
    pass


MAX_PAGES = 50


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _seed_user(db: Session, user_id: int) -> None:
    exists = db.scalar(
        select(DashboardLayout.id).where(DashboardLayout.user_id == user_id).limit(1)
    )
    if exists:
        return
    if SEED_PATH.exists():
        pages = json.loads(SEED_PATH.read_text(encoding="utf-8")).get("pages", [])
    else:
        pages = []
    try:
        with db.begin_nested():
            for index, page in enumerate(pages[:MAX_PAGES]):
                charts = json.loads(json.dumps(page.get("charts", [])[:50], ensure_ascii=False))
                db.add(
                    DashboardLayout(
                        user_id=user_id,
                        page_public_id=page.get("id") or _new_id(),
                        name=(page.get("name") or "새 레이아웃")[:80],
                        order_index=index,
                        charts=charts,
                    )
                )
            db.flush()
    except IntegrityError:
        # 동일 사용자의 첫 접근이 동시에 실행되면 unique 제약에서 한 쪽만 남긴다.
        return


def _rows(
    db: Session, user_id: int, *, for_update: bool = False
) -> list[DashboardLayout]:
    _seed_user(db, user_id)
    query = (
        select(DashboardLayout)
        .where(DashboardLayout.user_id == user_id)
        .order_by(DashboardLayout.order_index, DashboardLayout.id)
    )
    if for_update:
        query = query.with_for_update()
    return db.scalars(query).all()


def _find(
    db: Session, user_id: int, page_id: str, *, for_update: bool = False
) -> DashboardLayout:
    query = select(DashboardLayout).where(
        DashboardLayout.user_id == user_id,
        DashboardLayout.page_public_id == page_id,
    )
    if for_update:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise NotFound(page_id)
    return row


def _detail(row: DashboardLayout) -> dict:
    return {
        "id": row.page_public_id,
        "name": row.name,
        "charts": row.charts or [],
    }


def _summary(row: DashboardLayout) -> dict:
    return {
        "id": row.page_public_id,
        "name": row.name,
        "chart_count": len(row.charts or []),
    }


def list_pages(db: Session, user_id: int) -> list[dict]:
    return [_summary(row) for row in _rows(db, user_id)]


def get_page(db: Session, user_id: int, page_id: str) -> dict:
    _seed_user(db, user_id)
    return _detail(_find(db, user_id, page_id))


def create_page(
    db: Session, user_id: int, name: str, charts: list[dict] | None = None
) -> dict:
    rows = _rows(db, user_id, for_update=True)
    if len(rows) >= MAX_PAGES:
        raise LimitExceeded(f"레이아웃은 최대 {MAX_PAGES}개까지 만들 수 있습니다.")
    row = DashboardLayout(
        user_id=user_id,
        page_public_id=_new_id(),
        name=name.strip() or "새 레이아웃",
        order_index=len(rows),
        charts=charts or [],
    )
    db.add(row)
    db.flush()
    return _detail(row)


def update_page(
    db: Session,
    user_id: int,
    page_id: str,
    name: str | None = None,
    charts: list[dict] | None = None,
) -> dict:
    row = _find(db, user_id, page_id, for_update=True)
    if name is not None:
        row.name = name.strip() or row.name
    if charts is not None:
        row.charts = charts
    db.flush()
    return _detail(row)


def delete_page(db: Session, user_id: int, page_id: str) -> None:
    row = _find(db, user_id, page_id, for_update=True)
    db.delete(row)
    db.flush()
    for index, item in enumerate(_rows(db, user_id)):
        item.order_index = index


def duplicate_page(db: Session, user_id: int, page_id: str) -> dict:
    rows = _rows(db, user_id, for_update=True)
    if len(rows) >= MAX_PAGES:
        raise LimitExceeded(f"레이아웃은 최대 {MAX_PAGES}개까지 만들 수 있습니다.")
    source = _find(db, user_id, page_id, for_update=True)
    charts = json.loads(json.dumps(source.charts or [], ensure_ascii=False))
    for chart in charts:
        chart["id"] = _new_id()
    insert_at = source.order_index + 1
    for row in rows:
        if row.order_index >= insert_at:
            row.order_index += 1
    copy = DashboardLayout(
        user_id=user_id,
        page_public_id=_new_id(),
        name=source.name + " (사본)",
        order_index=insert_at,
        charts=charts,
    )
    db.add(copy)
    db.flush()
    return _detail(copy)


def reorder(db: Session, user_id: int, ids: list[str]) -> list[dict]:
    rows = _rows(db, user_id, for_update=True)
    by_id = {row.page_public_id: row for row in rows}
    if len(ids) != len(rows) or set(ids) != set(by_id):
        raise NotFound("reorder id 목록이 현재 페이지와 다릅니다")
    for index, page_id in enumerate(ids):
        by_id[page_id].order_index = index
    db.flush()
    return [_summary(by_id[page_id]) for page_id in ids]
