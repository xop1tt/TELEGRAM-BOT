import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from flamenco_bot.database.repository import (
    BookingCooldownError,
    BookingNotFoundError,
    CancellationWindowExpiredError,
    ClassBooking,
    ClassSlot,
    InsufficientLessonCreditsError,
    SlotUnavailableError,
    UserBooking,
)
from flamenco_bot.handlers.support import (
    deliver_support_reply,
    start_support,
    start_support_reply,
    submit_support_message,
)
from flamenco_bot.keyboards.user.lessons import (
    book_class_slot,
    cancel_booking_callback,
    request_booking_cancel,
    show_my_classes,
    show_slots,
)
from flamenco_bot.keyboards.user.main_menu import open_my_classes
from flamenco_bot.runtime.security import SupportRateLimiter
from flamenco_bot.studio_time import to_studio_time
from tests.support import (
    FakeCallback,
    FakeMessage,
    FakeRepository,
    FakeState,
    callback_data,
    inline_buttons,
)


def booking(slot_id, starts_at, class_key="beginner", status="confirmed"):
    return UserBooking(
        id=slot_id,
        slot_id=slot_id,
        class_key=class_key,
        starts_at=starts_at,
        booking_status=status,
        slot_status="open",
    )


def slot(slot_id, starts_at, class_key="beginner", capacity=8, booked=5, status="open"):
    return ClassSlot(
        id=slot_id,
        class_key=class_key,
        starts_at=starts_at,
        capacity=capacity,
        booked_count=booked,
        status=status,
    )


class SupportHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = FakeMessage(text="Вопрос о расписании")
        self.repository = FakeRepository()
        self.state = FakeState({"support_started_at": time.time()})
        self.limiter = SupportRateLimiter(max_messages=1)
        self.message.bot = SimpleNamespace(send_message=AsyncMock())

    async def test_support_message_is_stored_and_admins_are_notified(self):
        self.repository.list_admin_ids.return_value = [77]

        await submit_support_message(
            self.message,
            self.state,
            self.repository,
            self.limiter,
        )

        self.repository.create_support_message.assert_awaited_once_with(
            1001,
            "Вопрос о расписании",
        )
        self.message.bot.send_message.assert_awaited_once()
        self.assertEqual(self.message.bot.send_message.await_args.args[0], 77)
        self.assertIn(
            "Вопрос о расписании",
            self.message.bot.send_message.await_args.args[1],
        )
        self.assertIn("№1", self.message.last_answer.args[0])
        self.state.clear.assert_awaited_once()

    async def test_support_rate_limit_stops_repeated_messages(self):
        await submit_support_message(
            self.message,
            self.state,
            self.repository,
            self.limiter,
        )
        self.message.answer.reset_mock()
        self.message.text = "Ещё одно сообщение"
        await start_support(self.message, self.state)

        await submit_support_message(
            self.message,
            self.state,
            self.repository,
            self.limiter,
        )

        self.repository.create_support_message.assert_awaited_once()
        self.assertIn("Слишком много сообщений", self.message.last_answer.args[0])

    async def test_oversized_support_message_is_rejected_before_storage(self):
        self.message.text = "x" * 2001

        await submit_support_message(
            self.message,
            self.state,
            self.repository,
            self.limiter,
        )

        self.repository.create_support_message.assert_not_awaited()
        self.assertIn("2000 символов", self.message.last_answer.args[0])

    async def test_help_prompt_explains_where_the_answer_arrives(self):
        await start_support(self.message, self.state)
        self.assertEqual(self.state.current_state, "SupportForm:waiting_for_message")
        self.assertIn("ответ придёт в этот чат", self.message.last_answer.args[0])

    async def test_abandoned_support_input_does_not_create_a_ticket(self):
        self.state.data["support_started_at"] = time.time() - 31 * 60

        await submit_support_message(
            self.message,
            self.state,
            self.repository,
            self.limiter,
        )

        self.repository.create_support_message.assert_not_awaited()
        self.assertIsNone(self.state.current_state)
        self.assertIn("Сообщение не отправлено", self.message.last_answer.args[0])

    async def test_admin_reply_is_delivered_with_reply_button(self):
        # Права проверяет кнопка «Ответить» в админ-панели (test_admin_panel).
        result = await deliver_support_reply(
            self.message.bot, self.repository, 7, 1001, "Ответ"
        )

        self.repository.reply_support_ticket.assert_awaited_once_with(7, 1001, "Ответ")
        self.message.bot.send_message.assert_awaited_once()
        user_id, text = self.message.bot.send_message.await_args.args
        self.assertEqual(user_id, 1001)
        self.assertEqual(text, "Ответ службы поддержки по обращению №7:\nОтвет")
        # Под ответом — «Ответить», чтобы продолжить переписку.
        self.assertEqual(
            callback_data(
                self.message.bot.send_message.await_args.kwargs["reply_markup"]
            ),
            ["support_reply"],
        )
        self.assertIn("доставлен", result)

    async def test_reply_button_reopens_support_input(self):
        callback = FakeCallback("support_reply")

        await start_support_reply(callback, self.state)

        self.assertEqual(self.state.current_state, "SupportForm:waiting_for_message")
        self.assertIn("ответ придёт в этот чат", callback.message.last_answer.args[0])


class SlotsScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.state = FakeState()

    async def test_lists_nearest_available_classes_of_all_directions(self):
        soon = datetime.now(timezone.utc) + timedelta(days=2)
        self.repository.list_class_slots.return_value = [
            slot(5, soon, "beginner"),
            slot(6, soon + timedelta(days=1), "individual", capacity=1, booked=1),
            slot(7, soon + timedelta(days=2), "intermediate", status="closed"),
            slot(8, soon + timedelta(days=3), "intermediate"),
        ]
        callback = FakeCallback("slots:all")

        await show_slots(callback, self.repository, self.state)

        text, markup = callback.screen
        self.assertIn("Ближайшие занятия", text)
        self.assertIn("Баланс: 1 занятие", text)
        data = callback_data(markup)
        # Заполненные и закрытые занятия не предлагаются.
        self.assertIn("book:beginner:5", data)
        self.assertIn("book:intermediate:8", data)
        self.assertNotIn("book:individual:6", data)
        self.assertNotIn("book:intermediate:7", data)
        # Фильтр по направлению — на том же экране.
        self.assertTrue({"slots:all", "slots:beginner"} <= set(data))
        slot_button = next(
            button
            for button in inline_buttons(markup)
            if button.callback_data == "book:beginner:5"
        )
        self.assertIn("Начинающие", slot_button.text)
        self.assertIn("3 места", slot_button.text)
        self.assertIn(to_studio_time(soon).strftime("%H:%M"), slot_button.text)

    async def test_direction_filter_uses_direction_query(self):
        callback = FakeCallback("slots:individual")

        await show_slots(callback, self.repository, self.state)

        # Открытые занятия направления (и заполненные — для листа ожидания).
        self.repository.list_class_slots.assert_awaited_once_with(
            class_key="individual", limit=50
        )
        text, markup = callback.screen
        self.assertIn("Индивидуальное занятие", text)
        self.assertIn("Сейчас свободных занятий по этому направлению нет", text)
        self.assertIn("✓ Индивидуально", [b.text for b in inline_buttons(markup)])
        self.state.clear.assert_awaited_once()


class ClassBookingHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.state = FakeState()
        self.callback = FakeCallback("book:beginner:5")

    async def test_successful_booking_shows_details_balance_and_deadline(self):
        starts_at = datetime.now(timezone.utc) + timedelta(days=3)
        self.repository.list_admin_ids.return_value = [77]
        self.repository.book_class_slot.return_value = ClassBooking(
            id=12,
            slot_id=5,
            telegram_id=1001,
            starts_at=starts_at,
            class_key="beginner",
        )
        self.repository.get_lesson_credits.return_value = 3

        await book_class_slot(self.callback, self.repository, self.state)

        self.repository.book_class_slot.assert_awaited_once_with(
            5, 1001, notify_user=False
        )
        self.callback.answer.assert_awaited_once_with("Вы записаны!")
        text, markup = self.callback.screen
        for expected in (
            "Вы записаны",
            "Фламенко для начинающих",
            to_studio_time(starts_at).strftime("%d.%m"),
            to_studio_time(starts_at).strftime("%H:%M"),
            "Списано 1 занятие",
            "Баланс: 3 занятия",
            "Отменить запись можно до",
        ):
            self.assertIn(expected, text)
        self.assertIn("cancel_booking:5", callback_data(markup))
        # Администратору — уведомление о новой записи.
        self.callback.bot.send_message.assert_awaited_once()
        self.assertEqual(self.callback.bot.send_message.await_args.args[0], 77)
        self.assertIn("Новая запись", self.callback.bot.send_message.await_args.args[1])

    async def test_booking_within_24h_says_it_cannot_be_cancelled(self):
        starts_at = datetime.now(timezone.utc) + timedelta(hours=5)
        self.repository.book_class_slot.return_value = ClassBooking(
            id=12,
            slot_id=5,
            telegram_id=1001,
            starts_at=starts_at,
            class_key="beginner",
        )

        await book_class_slot(self.callback, self.repository, self.state)

        text, markup = self.callback.screen
        self.assertIn("Отменить эту запись уже нельзя", text)
        self.assertNotIn("cancel_booking:5", callback_data(markup))

    async def test_booking_within_24h_asks_for_confirmation_first(self):
        starts_at = datetime.now(timezone.utc) + timedelta(hours=5)
        self.repository.list_class_slots.return_value = [slot(5, starts_at)]

        await book_class_slot(self.callback, self.repository, self.state)

        self.repository.book_class_slot.assert_not_awaited()
        text, markup = self.callback.screen
        self.assertIn("Записаться на занятие?", text)
        self.assertIn("отменить эту запись будет нельзя", text)
        self.assertEqual(callback_data(markup), ["bookok:beginner:5", "slots:all"])

        self.repository.book_class_slot.return_value = ClassBooking(
            id=12,
            slot_id=5,
            telegram_id=1001,
            starts_at=starts_at,
            class_key="beginner",
        )
        confirmed = FakeCallback("bookok:beginner:5")
        await book_class_slot(confirmed, self.repository, self.state)

        self.repository.book_class_slot.assert_awaited_once_with(
            5, 1001, notify_user=False
        )
        text, _ = confirmed.screen
        self.assertIn("Вы записаны", text)

    async def test_cancellable_booking_needs_no_extra_confirmation(self):
        starts_at = datetime.now(timezone.utc) + timedelta(days=3)
        self.repository.list_class_slots.return_value = [slot(5, starts_at)]
        self.repository.book_class_slot.return_value = ClassBooking(
            id=12,
            slot_id=5,
            telegram_id=1001,
            starts_at=starts_at,
            class_key="beginner",
        )

        await book_class_slot(self.callback, self.repository, self.state)

        self.repository.book_class_slot.assert_awaited_once_with(
            5, 1001, notify_user=False
        )

    async def test_slot_taken_refreshes_the_list_without_success_message(self):
        self.repository.book_class_slot.side_effect = SlotUnavailableError("full")

        await book_class_slot(self.callback, self.repository, self.state)

        self.assertTrue(self.callback.answer.await_args.kwargs["show_alert"])
        text, _ = self.callback.screen
        self.assertIn("Список занятий обновлён", text)
        self.callback.bot.send_message.assert_not_awaited()

    async def test_no_credits_offers_packages_and_keeps_selected_class(self):
        self.repository.book_class_slot.side_effect = InsufficientLessonCreditsError(
            "На балансе нет доступных занятий."
        )

        await book_class_slot(self.callback, self.repository, self.state)

        text, markup = self.callback.screen
        self.assertIn("нужно 1 занятие на балансе", text)
        # Выбранное занятие (5) едет дальше по цепочке покупки.
        self.assertIn("pack:pack_4:5", callback_data(markup))
        self.callback.bot.send_message.assert_not_awaited()

    async def test_rebooking_cooldown_is_reported(self):
        self.repository.book_class_slot.side_effect = BookingCooldownError(
            "Повторная запись на это занятие после отмены будет доступна через 5 ч."
        )

        await book_class_slot(self.callback, self.repository, self.state)

        self.callback.answer.assert_awaited_once_with(
            "Повторная запись на это занятие после отмены будет доступна через 5 ч.",
            show_alert=True,
        )
        self.callback.message.edit_text.assert_not_awaited()

    async def test_malformed_callback_is_rejected(self):
        callback = FakeCallback("book:unknown:5")

        await book_class_slot(callback, self.repository, self.state)

        self.repository.book_class_slot.assert_not_awaited()
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])


class MyClassesHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.message = FakeMessage()
        self.state = FakeState()
        self.now = datetime.now(timezone.utc)

    async def test_lists_upcoming_classes_nearest_first_with_allowed_cancels(self):
        self.repository.list_bookings_for_telegram_id.return_value = [
            booking(9, self.now + timedelta(days=5), "intermediate"),
            booking(5, self.now + timedelta(days=1, hours=2)),
            booking(6, self.now - timedelta(days=1), "individual"),
            booking(7, self.now + timedelta(days=2), "individual", status="cancelled"),
            booking(8, self.now + timedelta(hours=3), "individual"),
        ]

        await open_my_classes(self.message, self.state, self.repository)

        text = self.message.last_answer.args[0]
        markup = self.message.last_answer.kwargs["reply_markup"]
        self.assertIn("Баланс: 1 занятие", text)
        # Ближайшее — первым.
        self.assertLess(text.index("Индивидуальное занятие"), text.index("Фламенко"))
        self.assertLess(text.index("Фламенко"), text.index("Продолжающая"))
        self.assertIn("(отмена уже недоступна)", text)
        data = callback_data(markup)
        # Отмена — только там, где ещё разрешена (не <24 ч, не прошедшие).
        self.assertEqual(
            [item for item in data if item.startswith("cancel_booking:")],
            ["cancel_booking:5", "cancel_booking:9"],
        )

    async def test_empty_state_offers_next_steps(self):
        await open_my_classes(self.message, self.state, self.repository)

        text = self.message.last_answer.args[0]
        self.assertIn("Предстоящих занятий пока нет", text)
        self.assertEqual(
            callback_data(self.message.last_answer.kwargs["reply_markup"]),
            ["slots:all", "packs:0", "hist:c:0"],
        )

    async def test_cancel_button_asks_for_confirmation_first(self):
        self.repository.list_bookings_for_telegram_id.return_value = [
            booking(5, self.now + timedelta(days=2))
        ]
        callback = FakeCallback("cancel_booking:5")

        await request_booking_cancel(callback, self.repository)

        self.repository.cancel_class_slot_booking.assert_not_awaited()
        text, markup = callback.screen
        self.assertIn("Отменить запись?", text)
        self.assertIn("12 часов", text)
        self.assertEqual(callback_data(markup), ["cancel_ok:5", "my"])

    async def test_cancel_request_for_late_booking_is_refused(self):
        self.repository.list_bookings_for_telegram_id.return_value = [
            booking(5, self.now + timedelta(hours=3))
        ]
        callback = FakeCallback("cancel_booking:5")

        await request_booking_cancel(callback, self.repository)

        self.assertIn("меньше 24 часов", callback.answer.await_args.args[0])
        text, _ = callback.screen
        self.assertIn("Мои занятия", text)

    async def test_confirmed_cancel_refunds_and_updates_message_with_balance(self):
        self.repository.list_bookings_for_telegram_id.return_value = [
            booking(5, self.now + timedelta(days=2))
        ]
        self.repository.cancel_class_slot_booking.return_value = True
        self.repository.get_lesson_credits.return_value = 2
        callback = FakeCallback("cancel_ok:5")

        await cancel_booking_callback(callback, self.repository)

        self.repository.cancel_class_slot_booking.assert_awaited_once_with(
            5, 1001, notify_user=False
        )
        callback.answer.assert_awaited_once_with("Запись отменена.")
        text, markup = callback.screen
        self.assertIn("Запись отменена", text)
        self.assertIn("Фламенко для начинающих", text)
        self.assertIn("Баланс: 2 занятия", text)
        self.assertEqual(callback_data(markup), ["my", "slots:all"])

    async def test_cancel_is_idempotent_for_already_cancelled(self):
        self.repository.cancel_class_slot_booking.return_value = False
        callback = FakeCallback("cancel_ok:5")

        await cancel_booking_callback(callback, self.repository)

        callback.answer.assert_awaited_once_with(
            "Эта запись уже отменена (вами или студией)."
        )
        text, _ = callback.screen
        self.assertIn("Мои занятия", text)

    async def test_cancel_reports_missing_booking(self):
        self.repository.cancel_class_slot_booking.side_effect = BookingNotFoundError(
            "Запись не найдена"
        )
        callback = FakeCallback("cancel_ok:5")

        await cancel_booking_callback(callback, self.repository)

        callback.answer.assert_awaited_once_with("Запись не найдена.", show_alert=True)

    async def test_cancel_reports_expired_deadline_from_repository(self):
        self.repository.cancel_class_slot_booking.side_effect = (
            CancellationWindowExpiredError(
                "Отмена доступна не позднее чем за 24 часа до начала занятия"
            )
        )
        callback = FakeCallback("cancel_ok:5")

        await cancel_booking_callback(callback, self.repository)

        callback.answer.assert_awaited_once_with(
            "Отмена доступна не позднее чем за 24 часа до начала занятия",
            show_alert=True,
        )

    async def test_back_from_confirmation_returns_to_my_classes(self):
        callback = FakeCallback("my")

        await show_my_classes(callback, self.repository, self.state)

        text, _ = callback.screen
        self.assertIn("Мои занятия", text)


if __name__ == "__main__":
    unittest.main()
