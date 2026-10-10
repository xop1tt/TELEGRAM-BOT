import unittest

from flamenco_bot.handlers import router as bot_router
from flamenco_bot.keyboards.user.account import router as account_keyboard_router
from flamenco_bot.keyboards.user.main_menu import router as main_menu_keyboard_router
from flamenco_bot.keyboards.user import (
    LEGACY_TO_SECTION,
    input_keyboard,
    main_menu_keyboard,
    phone_request_keyboard,
    profile_keyboard,
)


def labels(markup):
    return {button.text for row in markup.keyboard for button in row}


class KeyboardTests(unittest.TestCase):
    def test_all_menu_builders_return_reply_keyboards(self):
        for builder in (
            main_menu_keyboard,
            profile_keyboard,
            input_keyboard,
            phone_request_keyboard,
        ):
            with self.subTest(builder=builder.__name__):
                self.assertTrue(builder().keyboard)

    def test_main_menu_has_six_sections(self):
        self.assertEqual(
            labels(main_menu_keyboard()),
            {
                "📅 Расписание",
                "💃 Мои занятия",
                "🎟 Абонементы",
                "💳 Покупки",
                "👤 Профиль",
                "💬 Поддержка",
            },
        )

    def test_admin_menu_button_is_only_in_admin_main_menu(self):
        admin_labels = labels(main_menu_keyboard(is_admin=True))
        regular_labels = labels(main_menu_keyboard())
        self.assertIn("🛠 Админ-меню", admin_labels)
        self.assertIn("📊 Состояние бота", admin_labels)
        self.assertNotIn("🛠 Админ-меню", regular_labels)
        self.assertNotIn("📊 Состояние бота", regular_labels)

    def test_profile_keyboard_contains_edit_actions_and_home(self):
        self.assertEqual(
            labels(profile_keyboard()),
            {"✏️ Изменить имя", "📱 Изменить телефон", "🏠 Главное меню"},
        )

    def test_text_input_keyboards_offer_cancel_and_home(self):
        for builder in (input_keyboard, phone_request_keyboard):
            with self.subTest(builder=builder.__name__):
                self.assertIn("❌ Отмена", labels(builder()))
                self.assertIn("🏠 Главное меню", labels(builder()))

    def test_phone_keyboard_requests_contact(self):
        contact_button = phone_request_keyboard().keyboard[0][0]
        self.assertTrue(contact_button.request_contact)

    def test_legacy_labels_do_not_collide_with_current_menu(self):
        current = labels(main_menu_keyboard(is_admin=True)) | labels(profile_keyboard())
        self.assertFalse(current & set(LEGACY_TO_SECTION))

    def test_keyboard_handlers_precede_wildcard_fsm_handlers(self):
        account_names = [
            handler.callback.__name__
            for handler in account_keyboard_router.message.handlers
        ]
        main_menu_names = [
            handler.callback.__name__
            for handler in main_menu_keyboard_router.message.handlers
        ]
        self.assertLess(
            account_names.index("request_name"),
            account_names.index("save_phone"),
        )
        self.assertIn("cancel_from_menu", main_menu_names)
        self.assertIn("open_legacy_section", main_menu_names)

        # Кнопки меню обрабатываются раньше любых FSM-обработчиков ввода
        # текста (поддержка, профиль, защитный обработчик покупки).
        router_names = [child.name for child in bot_router.sub_routers]
        self.assertLess(
            router_names.index("main_menu_keyboard"),
            router_names.index("support"),
        )
        self.assertLess(
            router_names.index("admin_commands"),
            router_names.index("account_keyboard"),
        )
        self.assertLess(
            router_names.index("main_menu_keyboard"),
            router_names.index("purchases_keyboard"),
        )
