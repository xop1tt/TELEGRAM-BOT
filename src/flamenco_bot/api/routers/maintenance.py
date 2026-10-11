"""Техническое обслуживание сайта.

- ``GET /api/maintenance`` — публично: режим сайта (обычный, запланировано,
  идёт), тексты и время для страницы обслуживания. Без внутренних полей.
- ``/api/admin/maintenance/*`` — только администратору (``require_admin``,
  та же проверка роли в staff_members, что у остальной админки):
  планирование, изменение, досрочное включение, завершение, отмена,
  журнал событий, статистика доставки и предпросмотр (тестовый режим без
  записи и отправки).

Включение обслуживания не зависит от рассылки: сайт считает окно активным
по наступившему ``starts_at``, а уведомления ставятся отдельным
идемпотентным шагом (``process_maintenance``) — после действия и в фоне.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse

from ... import maintenance
from ...maintenance import (
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceWindow,
)
from ...notification_texts import render_notification
from ..dependencies import get_repository, require_admin, require_admin_actor
from ..schemas import (
    MAX_DB_ID,
    MaintenanceCreateRequest,
    MaintenanceDeliveryResponse,
    MaintenanceDetailResponse,
    MaintenanceEventResponse,
    MaintenanceOverviewResponse,
    MaintenancePreviewMessage,
    MaintenancePreviewResponse,
    MaintenanceStatusResponse,
    MaintenanceUpdateRequest,
    MaintenanceWarningPlanResponse,
    MaintenanceWindowResponse,
)

logger = logging.getLogger("bot.api.maintenance")

public_router = APIRouter(prefix="/api", tags=["maintenance"])
router = APIRouter(
    prefix="/api/admin/maintenance",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_id() -> Any:
    return Path(gt=0, le=MAX_DB_ID)


# ---------- публичное состояние ----------


@public_router.get("/maintenance", response_model=MaintenanceStatusResponse)
async def maintenance_status(
    repository: Any = Depends(get_repository),
) -> JSONResponse:
    now = _now()
    window = await repository.get_open_maintenance()
    mode = maintenance.site_mode(window, now)
    body = MaintenanceStatusResponse(
        mode=mode,
        server_time=now,
        retry_after=maintenance.retry_after_seconds(window, now),
    )
    if window is not None and mode != maintenance.MODE_NORMAL:
        body = body.model_copy(
            update={
                "title": window.title,
                "description": window.description,
                "starts_at": window.starts_at,
                "ends_at": window.ends_at,
            }
        )
    # Состояние меняется во времени — промежуточные кэши его не хранят.
    return JSONResponse(
        body.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
    )


# ---------- администрирование ----------


async def _follow_up(repository: Any) -> None:
    """Постановка уведомлений сразу после действия — по возможности.

    Ошибка здесь не отменяет действие: фоновая задача повторит шаг."""
    try:
        await repository.process_maintenance()
    except Exception:
        logger.exception("Maintenance follow-up processing failed")


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, MaintenanceNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, MaintenanceConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    if isinstance(error, PermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


_ACTION_ERRORS = (
    MaintenanceNotFoundError,
    MaintenanceConflictError,
    PermissionError,
    ValueError,
)


def _warning_plan(
    window: MaintenanceWindow, now: datetime, dispatches: List[dict]
) -> List[MaintenanceWarningPlanResponse]:
    handled = {
        item["stage"]: item
        for item in dispatches
        if item["revision"] == window.revision
    }
    plan = []
    for item in maintenance.plan_warnings(window, now):
        done = handled.get(maintenance.warning_stage(item.minutes))
        plan.append(
            MaintenanceWarningPlanResponse(
                minutes=item.minutes,
                due_at=item.due_at,
                state=item.state,
                recipients=done["recipients"] if done else None,
                skipped_reason=done["skipped_reason"] if done else None,
            )
        )
    return plan


async def _detail(
    repository: Any, window: MaintenanceWindow, now: datetime
) -> MaintenanceDetailResponse:
    stats: Dict[str, Dict[str, int]] = await repository.maintenance_delivery_stats(
        window.id
    )
    delivery = [
        MaintenanceDeliveryResponse(
            kind=kind,
            sent=counts.get("sent", 0),
            failed=counts.get("failed", 0),
            skipped=counts.get("skipped", 0),
            pending=counts.get("pending", 0) + counts.get("sending", 0),
        )
        for kind in maintenance.MAINTENANCE_KINDS
        if (counts := stats.get(kind))
    ]
    return MaintenanceDetailResponse(
        window=MaintenanceWindowResponse.from_window(window, now),
        warnings=_warning_plan(
            window, now, list(await repository.list_maintenance_dispatches(window.id))
        ),
        events=[
            MaintenanceEventResponse(**event)
            for event in await repository.list_maintenance_events(window.id)
        ],
        delivery=delivery,
    )


@router.get("", response_model=MaintenanceOverviewResponse)
async def maintenance_overview(
    repository: Any = Depends(get_repository),
) -> MaintenanceOverviewResponse:
    now = _now()
    current = await repository.get_open_maintenance()
    return MaintenanceOverviewResponse(
        current=await _detail(repository, current, now) if current else None,
        history=[
            MaintenanceWindowResponse.from_window(window, now)
            for window in await repository.list_maintenance(limit=20)
        ],
        audience_all=await repository.count_maintenance_audience("all"),
        audience_staff=await repository.count_maintenance_audience("staff"),
        server_time=now,
    )


@router.get("/{maintenance_id}", response_model=MaintenanceDetailResponse)
async def maintenance_detail(
    maintenance_id: int = _db_id(),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    window = await repository.get_maintenance(maintenance_id)
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Обслуживание не найдено")
    return await _detail(repository, window, _now())


@router.post(
    "",
    response_model=MaintenanceDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_maintenance(
    payload: MaintenanceCreateRequest,
    actor: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    try:
        window = await repository.create_maintenance(
            actor,
            payload.title,
            payload.description,
            payload.starts_at,
            payload.ends_at,
            payload.warning_minutes,
            notify_users=payload.notify_users,
            audience=payload.audience,
            start_now=payload.start_now,
        )
    except _ACTION_ERRORS as error:
        raise _http_error(error) from error
    await _follow_up(repository)
    return await _detail(
        repository, await repository.get_maintenance(window.id), _now()
    )


@router.patch("/{maintenance_id}", response_model=MaintenanceDetailResponse)
async def update_maintenance(
    payload: MaintenanceUpdateRequest,
    maintenance_id: int = _db_id(),
    actor: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    try:
        window = await repository.update_maintenance(
            maintenance_id, actor, payload.changes()
        )
    except _ACTION_ERRORS as error:
        raise _http_error(error) from error
    await _follow_up(repository)
    return await _detail(
        repository, await repository.get_maintenance(window.id), _now()
    )


async def _transition(
    action: str, maintenance_id: int, actor: int, repository: Any
) -> MaintenanceDetailResponse:
    try:
        window = await getattr(repository, action)(maintenance_id, actor)
    except _ACTION_ERRORS as error:
        raise _http_error(error) from error
    await _follow_up(repository)
    return await _detail(
        repository, await repository.get_maintenance(window.id), _now()
    )


@router.post("/{maintenance_id}/start", response_model=MaintenanceDetailResponse)
async def start_maintenance(
    maintenance_id: int = _db_id(),
    actor: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    return await _transition("start_maintenance", maintenance_id, actor, repository)


@router.post("/{maintenance_id}/complete", response_model=MaintenanceDetailResponse)
async def complete_maintenance(
    maintenance_id: int = _db_id(),
    actor: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    return await _transition("complete_maintenance", maintenance_id, actor, repository)


@router.post("/{maintenance_id}/cancel", response_model=MaintenanceDetailResponse)
async def cancel_maintenance(
    maintenance_id: int = _db_id(),
    actor: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> MaintenanceDetailResponse:
    return await _transition("cancel_maintenance", maintenance_id, actor, repository)


@router.post("/preview", response_model=MaintenancePreviewResponse)
async def preview_maintenance(
    payload: MaintenanceCreateRequest,
    repository: Any = Depends(get_repository),
) -> MaintenancePreviewResponse:
    """Тексты всех уведомлений и расписание предупреждений — без записи в БД
    и без отправки (проверка перед запуском)."""
    now = _now()
    starts_at = now if payload.start_now else payload.starts_at
    if starts_at is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Укажите время начала"
        )
    try:
        maintenance.validate_period(
            starts_at, payload.ends_at, now, future_start=not payload.start_now
        )
        minutes = maintenance.normalize_warning_minutes(payload.warning_minutes)
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    draft = MaintenanceWindow(
        id=0,
        status=maintenance.STATUS_SCHEDULED,
        title=payload.title,
        description=payload.description,
        starts_at=starts_at,
        ends_at=payload.ends_at,
        warning_minutes=minutes,
        notify_users=payload.notify_users,
        audience=payload.audience,
        revision=1,
        started_at=None,
        finished_at=None,
        created_by=0,
        created_at=now,
        updated_at=now,
    )
    messages = []
    for kind in maintenance.MAINTENANCE_KINDS:
        extra = {"phase": "scheduled"} if kind == maintenance.KIND_RESCHEDULED else {}
        rendered = render_notification(
            kind, maintenance.notification_payload(draft, **extra), now=now
        )
        messages.append(MaintenancePreviewMessage(kind=kind, text=rendered.text))
    return MaintenancePreviewResponse(
        messages=messages,
        warnings=_warning_plan(draft, now, []),
        recipients=await repository.count_maintenance_audience(payload.audience)
        if payload.notify_users
        else 0,
    )
