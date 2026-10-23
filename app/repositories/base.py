from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError


@dataclass
class PageParams:
    page: int = 1
    page_size: int = 20
    sort_by: str | None = None
    order: str = "desc"


def utc_day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def paginate(db: Session, stmt: Select, params: PageParams, sort_map: dict[str, Any], default_sort: str,
             tiebreaker: Any = None) -> tuple[list, int]:
    """Server-side pagination + sorting. `sort_map` whitelists sortable columns."""
    sort_key = params.sort_by or default_sort
    if sort_key not in sort_map:
        raise AppError(f"Invalid sort_by '{sort_key}'. Allowed: {', '.join(sorted(sort_map))}.",
                       code="VALIDATION_ERROR", status_code=422)
    col = sort_map[sort_key]
    ordering = [col.asc() if params.order == "asc" else col.desc()]
    if tiebreaker is not None:
        ordering.append(tiebreaker)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = db.scalars(stmt.order_by(*ordering).offset((params.page - 1) * params.page_size)
                       .limit(params.page_size)).unique().all()
    return list(items), total


def page_dict(items: list, total: int, params: PageParams) -> dict:
    return {"items": items, "total": total, "page": params.page, "page_size": params.page_size,
            "pages": math.ceil(total / params.page_size) if total else 0}


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:14].upper()}"
