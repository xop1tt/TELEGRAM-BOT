"""Подписи журнала действий — общие для админки бота и сайта.

Журнал объединяет при чтении (``list_audit_events``) строки, которые уже
лежат в своих таблицах: ledger (ручные операции с балансом и абонементами),
lesson_slot_events (расписание), lesson_payment_events (возвраты оплаты) и
staff_audit_log (сотрудники, профиль участника, каталог, настройки и т. п.).
"""

AUDIT_ACTION_LABELS = {
    "admin_adjustment": "корректировка баланса",
    "package_grant": "выдача абонемента",
    "package_revoke": "отзыв абонемента",
    "created": "создано занятие",
    "rescheduled": "перенос занятия",
    "capacity_changed": "вместимость",
    "closed": "запись закрыта",
    "reopened": "запись открыта",
    "cancelled": "отмена занятия",
    "refund_requested": "запрошен возврат оплаты",
    "refunded": "возврат оплаты",
    "refund_failed": "возврат отменён ЮKassa",
    "staff_role_set": "назначена роль сотрудника",
    "staff_removed": "снята роль сотрудника",
    "slot_teacher_set": "назначен преподаватель",
    "profile_updated": "изменён профиль участника",
    "class_format_created": "добавлено направление",
    "class_format_updated": "изменено направление",
    "lesson_package_created": "добавлен абонемент",
    "lesson_package_updated": "изменён абонемент",
    "studio_rules_updated": "изменены правила записи",
}


def audit_label(action: str) -> str:
    return AUDIT_ACTION_LABELS.get(action, action)
