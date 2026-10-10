"""Админ-панель сайта: ``/api/admin/*``.

Два роутера с общим префиксом:

- ``staff_router`` (``require_staff``) — расписание и поддержка: их видят
  и администратор, и преподаватель; преподаватель — только свои занятия
  (``lesson_slots.teacher_telegram_id``), остальное для него — 404;
- ``router`` (``require_admin``) — всё остальное: сводка, участники,
  платежи, сотрудники, создание занятий и назначение преподавателя.

Права — роль привязанного Telegram (staff_members); журналы в БД ведутся
по Telegram ID, как в админке бота. Логика — в существующих сервисах и
репозитории (те же, что у бота), здесь только HTTP-обёртка.
"""

import logging
from datetime import datetime, timezone

import asyncpg
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ...database.studio_models import (
    SlotNotFoundError,
    SlotStateError,
    SupportTicketStateError,
)
from ...services import AdminService, CatalogService, ScheduleService, SupportService
from ...services.support import (
    SupportReplyStatus,
    close_ticket_and_notify,
    deliver_support_reply,
)
from ...class_catalog import ClassFormat
from ...database.staff_postgres import LastAdminError
from ...payments.catalog import LessonPackage
from ..dependencies import (
    StaffActor,
    get_admin_service,
    get_bot,
    get_repository,
    get_schedule_service,
    require_admin,
    require_admin_actor,
    require_staff,
    require_staff_actor,
)
from ..schemas import (
    CATALOG_KEY_PATTERN,
    MAX_DB_ID,
    AdminClassFormat,
    AdminLessonPackage,
    StudioRulesModel,
    AdminAccountResponse,
    AdminBackgroundTaskResponse,
    AdminCancelSlotRequest,
    AdminCancelSlotResponse,
    AdminCreateSlotRequest,
    AdminDashboardResponse,
    AdminDatabaseStatsResponse,
    AdminMetricsResponse,
    AdminParticipantResponse,
    AdminProcessStatsResponse,
    AdminRequestStatsResponse,
    AdminSlotResponse,
    AdminSlotTeacherRequest,
    AdminSupportActionResponse,
    AdminSupportReplyRequest,
    AdminSupportThreadResponse,
    AdminSupportTicketResponse,
    AdminUpdateSlotRequest,
    StaffMemberResponse,
    StaffRoleRequest,
    SupportThreadResponse,
)


logger = logging.getLogger("bot.api.admin")
router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
staff_router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_staff)],
)


def _db_id() -> Any:
    # BIGINT: вне диапазона — 422 валидацией, а не 500 из драйвера БД.
    return Path(gt=0, le=MAX_DB_ID)


def _rub(amount_minor: int) -> float:
    return round(amount_minor / 100, 2)


# ---------- сводка и нагрузка ----------


@router.get("/dashboard", response_model=AdminDashboardResponse)
async def dashboard(
    admin_service: AdminService = Depends(get_admin_service),
) -> AdminDashboardResponse:
    data = await admin_service.dashboard()
    return AdminDashboardResponse(
        **{
            key: value
            for key, value in vars(data).items()
            if key not in {"sales_today_minor", "sales_month_minor"}
        },
        sales_today_rub=_rub(data.sales_today_minor),
        sales_month_rub=_rub(data.sales_month_minor),
    )


@router.get("/metrics", response_model=AdminMetricsResponse)
async def metrics(
    request: Request,
    repository: Any = Depends(get_repository),
    admin_service: AdminService = Depends(get_admin_service),
) -> AdminMetricsResponse:
    """Нагрузка для плавающего окна администратора (опрашивается раз в
    несколько секунд): HTTP-запросы, процесс API, база, фоновые задачи."""
    state = request.app.state
    requests = state.request_metrics.snapshot()
    process = await state.process_metrics.snapshot()
    database = await repository.get_database_metrics()
    summary = await admin_service.dashboard()
    tasks = []
    for name, attribute in (
        ("Уведомления участникам", "notification_task"),
        ("Сверка платежей ЮKassa", "reconciliation_task"),
    ):
        task = getattr(state, attribute, None)
        if task is not None:
            tasks.append(
                AdminBackgroundTaskResponse(name=name, running=not task.done())
            )
    return AdminMetricsResponse(
        collected_at=datetime.now(timezone.utc),
        requests=AdminRequestStatsResponse(**vars(requests)),
        process=AdminProcessStatsResponse(
            **{
                **vars(process),
                "load_average": list(process.load_average)
                if process.load_average
                else None,
            }
        ),
        database=AdminDatabaseStatsResponse(**vars(database)),
        background_tasks=tasks,
        online_users=summary.online_users,
        open_tickets=summary.open_tickets,
        pending_payments=summary.pending_payments,
    )


# ---------- расписание ----------


@staff_router.get("/slots", response_model=List[AdminSlotResponse])
async def list_slots(
    starts_after: Optional[datetime] = Query(default=None, alias="from"),
    starts_before: Optional[datetime] = Query(default=None, alias="to"),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> List[AdminSlotResponse]:
    """Без параметров — ближайшие занятия (и недавние); с ``from``/``to`` —
    архив за период, от новых к старым. Преподавателю — только свои."""
    teacher = None if staff.is_admin else staff.telegram_id
    if starts_after is None and starts_before is None:
        slots = await schedule.admin_schedule(limit=100, teacher_telegram_id=teacher)
    else:
        if (
            starts_after is None
            or starts_before is None
            or starts_after >= starts_before
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Укажите период: from раньше to",
            )
        slots = await schedule.archive(
            starts_after, starts_before, teacher_telegram_id=teacher, limit=100
        )
    return [AdminSlotResponse.from_record(slot) for slot in slots]


async def _slot_or_404(
    schedule: ScheduleService, slot_id: int, staff: Optional[StaffActor] = None
) -> Any:
    """Занятие или 404; чужое занятие преподавателю тоже не видно (404)."""
    slot = await schedule.get(slot_id)
    if slot is None or (
        staff is not None
        and not staff.is_admin
        and slot.teacher_telegram_id != staff.telegram_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Занятие не найдено")
    return slot


@router.post(
    "/slots",
    response_model=AdminSlotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_slot(
    payload: AdminCreateSlotRequest,
    admin_id: int = Depends(require_admin_actor),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> AdminSlotResponse:
    try:
        slot = await schedule.create(
            payload.class_key,
            payload.starts_at,
            payload.capacity,
            admin_id,
            teacher_telegram_id=payload.teacher_telegram_id,
        )
    except LookupError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return AdminSlotResponse.from_record(slot)


@staff_router.patch("/slots/{slot_id}", response_model=AdminSlotResponse)
async def update_slot(
    payload: AdminUpdateSlotRequest,
    slot_id: int = _db_id(),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> AdminSlotResponse:
    await _slot_or_404(schedule, slot_id, staff)
    admin_id = staff.telegram_id
    try:
        if payload.capacity is not None:
            await schedule.set_capacity(slot_id, payload.capacity, admin_id)
        if payload.starts_at is not None:
            await schedule.reschedule(
                slot_id, payload.starts_at, admin_id, payload.reason
            )
    except SlotNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except (SlotStateError, ValueError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return AdminSlotResponse.from_record(await _slot_or_404(schedule, slot_id))


@staff_router.post("/slots/{slot_id}/close", response_model=AdminSlotResponse)
async def close_slot(
    slot_id: int = _db_id(),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> AdminSlotResponse:
    await _slot_or_404(schedule, slot_id, staff)
    if not await schedule.close(slot_id, staff.telegram_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Запись на занятие уже закрыта")
    return AdminSlotResponse.from_record(await _slot_or_404(schedule, slot_id))


@staff_router.post("/slots/{slot_id}/reopen", response_model=AdminSlotResponse)
async def reopen_slot(
    slot_id: int = _db_id(),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> AdminSlotResponse:
    await _slot_or_404(schedule, slot_id, staff)
    if not await schedule.reopen(slot_id, staff.telegram_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Открыть запись можно только на закрытое будущее занятие",
        )
    return AdminSlotResponse.from_record(await _slot_or_404(schedule, slot_id))


@staff_router.post("/slots/{slot_id}/cancel", response_model=AdminCancelSlotResponse)
async def cancel_slot(
    payload: AdminCancelSlotRequest,
    slot_id: int = _db_id(),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> AdminCancelSlotResponse:
    """Отмена занятия студией: каждому записанному возвращается занятие и
    ставится уведомление (та же операция, что в админке бота)."""
    await _slot_or_404(schedule, slot_id, staff)
    try:
        result = await schedule.cancel(slot_id, staff.telegram_id, payload.reason)
    except SlotNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except (SlotStateError, ValueError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return AdminCancelSlotResponse(
        slot=AdminSlotResponse.from_record(result.slot),
        already_cancelled=result.already_cancelled,
        refunded_participants=len(result.refunds),
    )


@staff_router.get(
    "/slots/{slot_id}/participants",
    response_model=List[AdminParticipantResponse],
)
async def slot_participants(
    slot_id: int = _db_id(),
    staff: StaffActor = Depends(require_staff),
    schedule: ScheduleService = Depends(get_schedule_service),
) -> List[AdminParticipantResponse]:
    await _slot_or_404(schedule, slot_id, staff)
    participants = await schedule.participants(slot_id, include_cancelled=True)
    return [AdminParticipantResponse(**vars(item)) for item in participants]


@router.put("/slots/{slot_id}/teacher", response_model=AdminSlotResponse)
async def set_slot_teacher(
    payload: AdminSlotTeacherRequest,
    slot_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminSlotResponse:
    try:
        slot = await repository.set_slot_teacher(
            slot_id, payload.teacher_telegram_id, admin_id
        )
    except LookupError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    if slot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Занятие не найдено")
    return AdminSlotResponse.from_record(slot)


# ---------- каталог и правила записи ----------


def _catalog_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, asyncpg.CheckViolationError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Недопустимые значения полей"
        )
    return HTTPException(status.HTTP_409_CONFLICT, str(error))


@router.get("/classes", response_model=List[AdminClassFormat])
async def list_class_formats(
    repository: Any = Depends(get_repository),
) -> List[AdminClassFormat]:
    return [
        AdminClassFormat(**vars(item))
        for item in await CatalogService(repository).class_formats()
    ]


@router.post(
    "/classes", response_model=AdminClassFormat, status_code=status.HTTP_201_CREATED
)
async def create_class_format(
    payload: AdminClassFormat,
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminClassFormat:
    try:
        saved = await CatalogService(repository).save_class_format(
            ClassFormat(**payload.model_dump()), admin_id, create=True
        )
    except (ValueError, LookupError, asyncpg.CheckViolationError) as error:
        raise _catalog_error(error) from error
    return AdminClassFormat(**vars(saved))


@router.put("/classes/{key}", response_model=AdminClassFormat)
async def update_class_format(
    payload: AdminClassFormat,
    key: str = Path(pattern=CATALOG_KEY_PATTERN),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminClassFormat:
    if payload.key != key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Ключ направления менять нельзя"
        )
    try:
        saved = await CatalogService(repository).save_class_format(
            ClassFormat(**payload.model_dump()), admin_id, create=False
        )
    except (ValueError, LookupError, asyncpg.CheckViolationError) as error:
        raise _catalog_error(error) from error
    return AdminClassFormat(**vars(saved))


@router.get("/packages", response_model=List[AdminLessonPackage])
async def list_lesson_packages(
    repository: Any = Depends(get_repository),
) -> List[AdminLessonPackage]:
    return [
        AdminLessonPackage(**vars(item))
        for item in await CatalogService(repository).packages()
    ]


@router.post(
    "/packages",
    response_model=AdminLessonPackage,
    status_code=status.HTTP_201_CREATED,
)
async def create_lesson_package(
    payload: AdminLessonPackage,
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminLessonPackage:
    try:
        saved = await CatalogService(repository).save_package(
            LessonPackage(**payload.model_dump()), admin_id, create=True
        )
    except (ValueError, LookupError, asyncpg.CheckViolationError) as error:
        raise _catalog_error(error) from error
    return AdminLessonPackage(**vars(saved))


@router.put("/packages/{key}", response_model=AdminLessonPackage)
async def update_lesson_package(
    payload: AdminLessonPackage,
    key: str = Path(pattern=CATALOG_KEY_PATTERN),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> AdminLessonPackage:
    if payload.key != key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Ключ абонемента менять нельзя"
        )
    try:
        saved = await CatalogService(repository).save_package(
            LessonPackage(**payload.model_dump()), admin_id, create=False
        )
    except (ValueError, LookupError, asyncpg.CheckViolationError) as error:
        raise _catalog_error(error) from error
    return AdminLessonPackage(**vars(saved))


@router.get("/settings", response_model=StudioRulesModel)
async def read_studio_rules(
    repository: Any = Depends(get_repository),
) -> StudioRulesModel:
    return StudioRulesModel(**vars(await repository.get_studio_rules()))


@router.put("/settings", response_model=StudioRulesModel)
async def update_studio_rules(
    payload: StudioRulesModel,
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> StudioRulesModel:
    updated = await CatalogService(repository).update_rules(
        admin_id, **payload.model_dump()
    )
    return StudioRulesModel(**vars(updated))


# ---------- сотрудники ----------


def _staff_response(member: Any) -> StaffMemberResponse:
    return StaffMemberResponse(**vars(member), public_name=member.public_name)


@router.get("/staff", response_model=List[StaffMemberResponse])
async def list_staff(
    repository: Any = Depends(get_repository),
) -> List[StaffMemberResponse]:
    return [_staff_response(member) for member in await repository.list_staff()]


@router.put("/staff/{telegram_id}", response_model=StaffMemberResponse)
async def set_staff_role(
    payload: StaffRoleRequest,
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> StaffMemberResponse:
    try:
        member = await repository.set_staff_role(
            telegram_id, payload.role, admin_id, payload.display_name
        )
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except LastAdminError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from error
    return _staff_response(member)


@router.delete("/staff/{telegram_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_staff(
    telegram_id: int = _db_id(),
    admin_id: int = Depends(require_admin_actor),
    repository: Any = Depends(get_repository),
) -> None:
    try:
        removed = await repository.remove_staff(telegram_id, admin_id)
    except LastAdminError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сотрудник не найден")


# ---------- пользователи и платежи ----------


@router.get("/users", response_model=List[AdminAccountResponse])
async def list_users(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    repository: Any = Depends(get_repository),
) -> List[AdminAccountResponse]:
    accounts = await repository.search_admin_accounts(q, limit)
    return [AdminAccountResponse(**vars(account)) for account in accounts]


# ---------- поддержка ----------


@staff_router.get("/support", response_model=List[AdminSupportTicketResponse])
async def list_support_tickets(
    ticket_status: str = Query(
        default="open", alias="status", pattern="^(open|closed)$"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    repository: Any = Depends(get_repository),
) -> List[AdminSupportTicketResponse]:
    tickets = await repository.list_admin_support_tickets(ticket_status, limit)
    return [AdminSupportTicketResponse(**vars(ticket)) for ticket in tickets]


@staff_router.get("/support/{ticket_id}", response_model=AdminSupportThreadResponse)
async def read_support_ticket(
    ticket_id: int = _db_id(),
    repository: Any = Depends(get_repository),
) -> AdminSupportThreadResponse:
    thread = await repository.get_support_ticket_thread(ticket_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Обращение не найдено")
    return AdminSupportThreadResponse(
        **SupportThreadResponse.from_thread(thread).model_dump(),
        telegram_id=thread.telegram_id,
        user_name=thread.user_name,
    )


@staff_router.post(
    "/support/{ticket_id}/reply", response_model=AdminSupportActionResponse
)
async def reply_support_ticket(
    payload: AdminSupportReplyRequest,
    ticket_id: int = _db_id(),
    admin_id: int = Depends(require_staff_actor),
    repository: Any = Depends(get_repository),
    bot: Any = Depends(get_bot),
) -> AdminSupportActionResponse:
    body = payload.body.strip()
    if not body:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Ответ не может быть пустым"
        )
    result = await deliver_support_reply(bot, repository, ticket_id, admin_id, body)
    if result is SupportReplyStatus.NOT_FOUND:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Открытое обращение с таким номером не найдено"
        )
    return AdminSupportActionResponse(status=result.value)


@staff_router.post(
    "/support/{ticket_id}/reopen", response_model=AdminSupportActionResponse
)
async def reopen_support_ticket(
    ticket_id: int = _db_id(),
    admin_id: int = Depends(require_staff_actor),
    repository: Any = Depends(get_repository),
) -> AdminSupportActionResponse:
    """Открыть закрытое обращение снова (как «↩️ Открыть снова» в боте)."""
    try:
        reopened = await SupportService(repository, None, None).reopen(
            ticket_id, admin_id
        )
    except SupportTicketStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    if reopened is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Обращение не найдено")
    return AdminSupportActionResponse(status="open")


@staff_router.post(
    "/support/{ticket_id}/close", response_model=AdminSupportActionResponse
)
async def close_support_ticket(
    ticket_id: int = _db_id(),
    admin_id: int = Depends(require_staff_actor),
    repository: Any = Depends(get_repository),
    bot: Any = Depends(get_bot),
) -> AdminSupportActionResponse:
    if not await close_ticket_and_notify(bot, repository, ticket_id, admin_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Открытое обращение с таким номером не найдено"
        )
    return AdminSupportActionResponse(status="closed")


__all__ = ["router", "staff_router"]
