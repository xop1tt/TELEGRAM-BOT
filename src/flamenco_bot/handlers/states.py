from aiogram.fsm.state import State, StatesGroup


class AccountForm(StatesGroup):
    waiting_for_phone = State()
    waiting_for_phone_code = State()
    waiting_for_name = State()


class LessonForm(StatesGroup):
    # Прежнее текстовое подтверждение покупки. Новая версия в это состояние
    # не переводит (покупка — только кнопкой «Оплатить»); состояние оставлено,
    # чтобы защитный обработчик сбросил его у старой сессии, не создавая платёж.
    waiting_for_purchase_confirmation = State()


class SupportForm(StatesGroup):
    waiting_for_message = State()


class AdminForm(StatesGroup):
    waiting_for_search = State()
    waiting_for_name_value = State()
    waiting_for_phone_value = State()
    waiting_for_credit_adjustment_confirmation = State()
    # Админ-панель (handlers/admin_panel.py): ввод текста в сценариях.
    waiting_for_slot_time = State()
    waiting_for_slot_capacity = State()
    waiting_for_reschedule_time = State()
    waiting_for_cancel_reason = State()
    waiting_for_capacity_change = State()
    waiting_for_grant_reason = State()
    waiting_for_revoke_reason = State()
    waiting_for_support_reply = State()
    waiting_for_client_adjustment = State()
    waiting_for_booking_search = State()
    # Настройки студии (handlers/admin_settings.py).
    waiting_for_catalog_value = State()
    waiting_for_rule_value = State()
    waiting_for_staff_id = State()
    waiting_for_staff_name = State()
    waiting_for_note = State()
    waiting_for_broadcast_text = State()
