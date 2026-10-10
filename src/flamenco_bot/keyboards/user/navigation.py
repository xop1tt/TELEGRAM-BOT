from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...runtime.admin_access import get_admin_id
from ...handlers.states import AccountForm, AdminForm
from . import main_menu_keyboard
from ..admin import admin_menu_keyboard


async def cancel_current_action(
    message: Message,
    state: FSMContext,
    repository: Any,
) -> None:
    """«❌ Отмена» / /cancel: прервать ввод и вернуться туда, откуда он начат."""
    current_state = await state.get_state()
    await state.clear()

    if current_state in {
        AccountForm.waiting_for_phone.state,
        AccountForm.waiting_for_phone_code.state,
        AccountForm.waiting_for_name.state,
    }:
        text = "Ввод отменён. Профиль не изменён — он в «👤 Профиль»."
        reply_markup = main_menu_keyboard(
            is_admin=await get_admin_id(message, repository) is not None
        )
    elif (
        current_state
        in {
            AdminForm.waiting_for_search.state,
            AdminForm.waiting_for_name_value.state,
            AdminForm.waiting_for_phone_value.state,
            AdminForm.waiting_for_slot_time.state,
            AdminForm.waiting_for_slot_capacity.state,
            AdminForm.waiting_for_reschedule_time.state,
            AdminForm.waiting_for_cancel_reason.state,
            AdminForm.waiting_for_capacity_change.state,
            AdminForm.waiting_for_grant_reason.state,
            AdminForm.waiting_for_revoke_reason.state,
            AdminForm.waiting_for_support_reply.state,
            AdminForm.waiting_for_client_adjustment.state,
        }
        and await get_admin_id(message, repository) is not None
    ):
        text = "Действие отменено."
        reply_markup = admin_menu_keyboard()
    else:
        text = (
            "Ввод отменён. Вы в главном меню."
            if current_state is not None
            else "Отменять нечего. Вы в главном меню."
        )
        reply_markup = main_menu_keyboard(
            is_admin=await get_admin_id(message, repository) is not None
        )

    await message.answer(text, reply_markup=reply_markup)
