"""Лист ожидания участника: ``/api/waitlist``.

Встать в очередь — на заполненное открытое занятие, с занятием на балансе;
когда место освободится, придёт уведомление (в Telegram и на сайте), а
записаться — обычным ``POST /api/bookings``. Логика — в репозитории
(``waitlist_postgres``), общая с ботом.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, status

from ...database.repository import (
    InsufficientLessonCreditsError,
    SlotUnavailableError,
    WebUserRecord,
)
from ...database.waitlist_postgres import AlreadyBookedError
from ..dependencies import get_repository, require_telegram_linked_user
from ..schemas import MAX_DB_ID, BookingRequest, WaitlistEntryResponse

router = APIRouter(prefix="/api/waitlist", tags=["waitlist"])


@router.get("/me", response_model=List[WaitlistEntryResponse])
async def my_waitlist(
    current_user: WebUserRecord = Depends(require_telegram_linked_user),
    repository=Depends(get_repository),
) -> List[WaitlistEntryResponse]:
    entries = await repository.list_user_waitlist(current_user.telegram_id)
    return [WaitlistEntryResponse.from_entry(entry) for entry in entries]


@router.post(
    "", response_model=WaitlistEntryResponse, status_code=status.HTTP_201_CREATED
)
async def join_waitlist(
    payload: BookingRequest,
    current_user: WebUserRecord = Depends(require_telegram_linked_user),
    repository=Depends(get_repository),
) -> WaitlistEntryResponse:
    try:
        entry = await repository.join_waitlist(
            payload.slot_id, current_user.telegram_id
        )
    except (
        SlotUnavailableError,
        AlreadyBookedError,
        InsufficientLessonCreditsError,
    ) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return WaitlistEntryResponse.from_entry(entry)


@router.delete("/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def leave_waitlist(
    slot_id: int = Path(gt=0, le=MAX_DB_ID),
    current_user: WebUserRecord = Depends(require_telegram_linked_user),
    repository=Depends(get_repository),
) -> None:
    if not await repository.leave_waitlist(slot_id, current_user.telegram_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вас нет в этой очереди")
