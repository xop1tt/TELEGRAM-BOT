"""Админка сайта: рассылки, заметки, сбои уведомлений, отчёты, выгрузки.

- Рассылки — сотрудникам: администратору — любой сегмент, преподавателю —
  только участникам своего занятия (сегмент ``slot``).
- Заметки об участниках, сбои уведомлений, отчёты и выгрузки CSV — только
  администратору.
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import Response

from ...class_catalog import CLASS_LABELS
from ...database.engagement_postgres import validate_segment
from ..dependencies import (
    StaffActor,
    get_repository,
    require_admin,
    require_admin_actor,
    require_staff,
)
from ..schemas import (
    MAX_DB_ID,
    AdminBroadcastRequest,
    AdminBroadcastResponse,
    AdminFailedNotificationResponse,
    AdminFailedNotificationsPage,
    AdminNoteRequest,
    AdminNoteResponse,
    AdminSegmentCountResponse,
    AdminSegmentRequest,
)

staff_router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_staff)]
)
router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


def _db_id() -> Any:
    return Path(gt=0, le=MAX_DB_ID)


async def _check_segment_access(
    staff: StaffActor, segment: str, params: dict, repository: Any
) -> dict:
    try:
        clean = validate_segment(segment, params)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    if staff.is_admin:
        return clean
    if segment != "slot" or not await repository.is_slot_teacher(
        clean["slot_id"], staff.telegram_id
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Преподаватель пишет только участникам своего занятия",
        )
    return clean


# ---------- рассылки ----------


@staff_router.post("/broadcasts/preview", response_model=AdminSegmentCountResponse)
async def preview_broadcast(
    payload: AdminSegmentRequest,
    staff: StaffActor = Depends(require_staff),
    repository: Any = Depends(get_repository),
) -> AdminSegmentCountResponse:
    clean = await _check_segment_access(
        staff, payload.segment, payload.params, repository
    )
    return AdminSegmentCountResponse(
        recipients=await repository.count_segment(payload.segment, clean)
    )


@staff_router.post(
    "/broadcasts",
    response_model=AdminBroadcastResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_broadcast(
    payload: AdminBroadcastRequest,
    staff: StaffActor = Depends(require_staff),
    repository: Any = Depends(get_repository),
) -> AdminBroadcastResponse:
    clean = await _check_segment_access(
        staff, payload.segment, payload.params, repository
    )
    try:
        broadcast = await repository.create_broadcast(
            staff.telegram_id,
            payload.body,
            payload.segment,
            clean,
            author_is_admin_required=staff.is_admin,
        )
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminBroadcastResponse(**vars(broadcast))


@router.get("/broadcasts", response_model=List[AdminBroadcastResponse])
async def list_broadcasts(
    repository: Any = Depends(get_repository),
) -> List[AdminBroadcastResponse]:
    return [
        AdminBroadcastResponse(**vars(item))
        for item in await repository.list_broadcasts(limit=30)
    ]


# ---------- заметки ----------


@router.get("/clients/{telegram_id}/notes", response_model=List[AdminNoteResponse])
async def list_notes(
    telegram_id: int = _db_id(), repository: Any = Depends(get_repository)
) -> List[AdminNoteResponse]:
    return [
        AdminNoteResponse(**vars(note))
        for note in await repository.list_client_notes(telegram_id)
    ]


@router.post(
    "/clients/{telegram_id}/notes",
    response_model=AdminNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_note(
    payload: AdminNoteRequest,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminNoteResponse:
    try:
        note = await repository.add_client_note(telegram_id, admin_id, payload.body)
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminNoteResponse(**vars(note))


@router.delete(
    "/clients/{telegram_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_note(
    telegram_id: int = _db_id(),
    note_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> None:
    if not await repository.delete_client_note(telegram_id, note_id, admin_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заметка не найдена")


# ---------- сбои уведомлений ----------


@router.get("/notifications/failed", response_model=AdminFailedNotificationsPage)
async def failed_notifications(
    before_id: Optional[int] = Query(default=None, gt=0, le=MAX_DB_ID),
    limit: int = Query(default=50, ge=1, le=200),
    repository: Any = Depends(get_repository),
) -> AdminFailedNotificationsPage:
    items = list(
        await repository.list_failed_notifications(limit + 1, before_id=before_id)
    )
    has_more = len(items) > limit
    items = items[:limit]
    return AdminFailedNotificationsPage(
        stats=await repository.notification_delivery_stats(30),
        items=[AdminFailedNotificationResponse(**vars(item)) for item in items],
        next_before_id=items[-1].id if has_more and items else None,
    )


# ---------- отчёты и выгрузки ----------


def _period(start: Optional[datetime], end: Optional[datetime]) -> tuple:
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(days=90)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Время — с часовым поясом"
        )
    if start >= end or end - start > timedelta(days=731):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Период: начало раньше конца, не больше двух лет",
        )
    return start, end


@router.get("/reports")
async def report(
    start: Optional[datetime] = Query(default=None, alias="from"),
    end: Optional[datetime] = Query(default=None, alias="to"),
    sleeping_days: int = Query(default=30, ge=1, le=365),
    repository: Any = Depends(get_repository),
) -> dict:
    start, end = _period(start, end)
    data = await repository.get_report(start, end, sleeping_days)
    for row in data["fill_by_direction"]:
        row["label"] = CLASS_LABELS[row["key"]]
    data["from"] = start
    data["to"] = end
    return data


EXPORT_KINDS = ("payments", "bookings", "clients")

# Ячейка, начинающаяся с этих символов, в Excel/LibreOffice — формула. Имя
# и телефон участник задаёт сам (бот, сайт), поэтому без экранирования
# «=HYPERLINK(...)» в имени выполнится у администратора при открытии
# выгрузки (CSV injection). Апостроф впереди делает ячейку текстом.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


@router.get("/export/{kind}.csv")
async def export_csv(
    kind: str = Path(pattern="^(payments|bookings|clients)$"),
    start: Optional[datetime] = Query(default=None, alias="from"),
    end: Optional[datetime] = Query(default=None, alias="to"),
    repository: Any = Depends(get_repository),
) -> Response:
    """CSV для Excel: UTF-8 с BOM, разделитель «;»."""
    start, end = _period(start, end)
    rows = await repository.export_rows(kind, start, end)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    if rows:
        writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow([_csv_cell(value) for value in row.values()])
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="{}-{}.csv"'.format(
                kind, end.strftime("%Y%m%d")
            )
        },
    )
