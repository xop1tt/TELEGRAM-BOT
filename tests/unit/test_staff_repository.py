"""Сотрудники студии на PostgreSQL: роли, защита последнего администратора,
преподаватель занятия, журнал действий.

Права — на уровне репозитория: даже если обработчик забудет проверку, чужой
запрос не должен менять роли. Отказ не оставляет побочных эффектов.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from flamenco_bot.database.staff_postgres import (
    LastAdminError,
    normalize_display_name,
)
from tests.pg import PostgresTestCase, fetchval, make_admin

ADMIN = 77
OTHER_ADMIN = 78
TEACHER = 55
USER = 1001


class NormalizeDisplayNameTests(unittest.TestCase):
    def test_spaces_are_collapsed_and_blank_means_none(self):
        self.assertEqual(normalize_display_name("  Мария   Лопес "), "Мария Лопес")
        self.assertIsNone(normalize_display_name("   "))
        self.assertIsNone(normalize_display_name(None))

    def test_too_long_name_is_rejected(self):
        self.assertEqual(normalize_display_name("я" * 64), "я" * 64)
        with self.assertRaises(ValueError):
            normalize_display_name("я" * 65)


class StaffRoleTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN)
        await self.repository.get_or_create_profile(TEACHER, "Мария")
        await self.repository.get_or_create_profile(USER, "Анна")

    async def _actions(self):
        return [e.action for e in await self.repository.list_staff_audit()]

    async def test_admin_assigns_teacher_with_schedule_name_and_audit(self):
        member = await self.repository.set_staff_role(
            TEACHER, "teacher", ADMIN, "  Мария   Л. "
        )
        self.assertEqual((member.role, member.display_name), ("teacher", "Мария Л."))
        self.assertEqual(await self.repository.get_staff_role(TEACHER), "teacher")
        self.assertIn("staff_role_set", await self._actions())
        listed = {m.telegram_id: m.role for m in await self.repository.list_staff()}
        self.assertEqual(listed, {ADMIN: "admin", TEACHER: "teacher"})
        self.assertEqual(
            (await self.repository.get_staff_member(TEACHER)).role, "teacher"
        )
        self.assertIsNone(await self.repository.get_staff_member(USER))

    async def test_only_an_admin_can_change_roles(self):
        for actor in (USER, TEACHER):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                await self.repository.set_staff_role(USER, "admin", actor)
        await self.repository.set_staff_role(TEACHER, "teacher", ADMIN)
        with self.assertRaises(PermissionError):
            await self.repository.set_staff_role(USER, "teacher", TEACHER)
        self.assertIsNone(await self.repository.get_staff_role(USER))

    async def test_unknown_role_and_unknown_person_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.repository.set_staff_role(TEACHER, "owner", ADMIN)
        with self.assertRaises(LookupError):
            await self.repository.set_staff_role(999999, "teacher", ADMIN)
        self.assertEqual(await self._actions(), [])

    async def test_last_admin_cannot_be_demoted_or_removed(self):
        with self.assertRaises(LastAdminError):
            await self.repository.set_staff_role(ADMIN, "teacher", ADMIN)
        with self.assertRaises(LastAdminError):
            await self.repository.remove_staff(ADMIN, ADMIN)
        self.assertEqual(await self.repository.get_staff_role(ADMIN), "admin")

    async def test_admin_can_be_demoted_once_another_admin_exists(self):
        await make_admin(self.repository, OTHER_ADMIN, "Второй")
        await self.repository.set_staff_role(ADMIN, "teacher", OTHER_ADMIN)
        self.assertEqual(await self.repository.get_staff_role(ADMIN), "teacher")
        with self.assertRaises(LastAdminError):
            await self.repository.remove_staff(OTHER_ADMIN, OTHER_ADMIN)

    async def test_two_admins_cannot_remove_each_other_concurrently(self):
        """Оба одновременно снимают друг друга: должен остаться один."""
        await make_admin(self.repository, OTHER_ADMIN, "Второй")

        async def remove(target, actor):
            try:
                return await self.repository.remove_staff(target, actor)
            except (LastAdminError, PermissionError):
                return None

        await asyncio.gather(remove(ADMIN, OTHER_ADMIN), remove(OTHER_ADMIN, ADMIN))
        admins = [m for m in await self.repository.list_staff() if m.role == "admin"]
        self.assertGreaterEqual(len(admins), 1)

    async def test_removal_reports_whether_anything_changed_and_audits(self):
        await self.repository.set_staff_role(TEACHER, "teacher", ADMIN)
        self.assertTrue(await self.repository.remove_staff(TEACHER, ADMIN))
        self.assertFalse(await self.repository.remove_staff(TEACHER, ADMIN))
        self.assertEqual((await self._actions()).count("staff_removed"), 1)
        with self.assertRaises(PermissionError):
            await self.repository.remove_staff(ADMIN, USER)

    async def test_removed_teacher_keeps_slots_but_loses_the_assignment(self):
        await self.repository.set_staff_role(TEACHER, "teacher", ADMIN)
        slot = await self.repository.create_class_slot(
            "beginner",
            datetime.now(timezone.utc) + timedelta(days=2),
            5,
            ADMIN,
            teacher_telegram_id=TEACHER,
        )
        await self.repository.remove_staff(TEACHER, ADMIN)
        self.assertIsNotNone(await self.repository.get_class_slot(slot.id))
        self.assertFalse(await self.repository.is_slot_teacher(slot.id, TEACHER))


class SlotTeacherTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN)
        await self.repository.get_or_create_profile(TEACHER, "Мария")
        await self.repository.set_staff_role(TEACHER, "teacher", ADMIN)
        await self.repository.get_or_create_profile(USER, "Анна")
        self.slot = await self.repository.create_class_slot(
            "beginner", datetime.now(timezone.utc) + timedelta(days=2), 5, ADMIN
        )

    async def test_admin_assigns_and_clears_the_teacher(self):
        updated = await self.repository.set_slot_teacher(self.slot.id, TEACHER, ADMIN)
        self.assertEqual(updated.teacher_telegram_id, TEACHER)
        self.assertTrue(await self.repository.is_slot_teacher(self.slot.id, TEACHER))
        self.assertFalse(await self.repository.is_slot_teacher(self.slot.id, USER))

        cleared = await self.repository.set_slot_teacher(self.slot.id, None, ADMIN)
        self.assertIsNone(cleared.teacher_telegram_id)
        self.assertFalse(await self.repository.is_slot_teacher(self.slot.id, TEACHER))

    async def test_assignment_is_audited_with_previous_teacher(self):
        await self.repository.set_slot_teacher(self.slot.id, TEACHER, ADMIN)
        await self.repository.set_slot_teacher(self.slot.id, None, ADMIN)
        entries = [
            e
            for e in await self.repository.list_staff_audit()
            if e.action == "slot_teacher_set"
        ]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].details["previous_teacher"], TEACHER)

    async def test_non_admin_cannot_assign(self):
        for actor in (TEACHER, USER):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                await self.repository.set_slot_teacher(self.slot.id, TEACHER, actor)
        self.assertFalse(await self.repository.is_slot_teacher(self.slot.id, TEACHER))

    async def test_only_staff_can_be_teachers_and_missing_slot_is_none(self):
        with self.assertRaises(LookupError):
            await self.repository.set_slot_teacher(self.slot.id, USER, ADMIN)
        self.assertIsNone(
            await self.repository.set_slot_teacher(999999, TEACHER, ADMIN)
        )
        self.assertIsNone(
            await fetchval(
                self.repository,
                "SELECT teacher_telegram_id FROM lesson_slots WHERE id = $1",
                self.slot.id,
            )
        )


class StaffAuditTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await make_admin(self.repository, ADMIN)

    async def test_log_is_newest_first_filtered_and_paged(self):
        for index in range(5):
            await self.repository.record_staff_action(
                ADMIN,
                "note_{}".format(index),
                USER if index % 2 else None,
                {"i": index},
            )
        everything = await self.repository.list_staff_audit()
        self.assertEqual([e.action for e in everything][:2], ["note_4", "note_3"])
        page = await self.repository.list_staff_audit(limit=2)
        older = await self.repository.list_staff_audit(limit=2, before_id=page[-1].id)
        self.assertEqual([e.action for e in older], ["note_2", "note_1"])
        only_user = await self.repository.list_staff_audit(target_telegram_id=USER)
        self.assertEqual({e.action for e in only_user}, {"note_1", "note_3"})

    async def test_limit_is_validated_and_details_roundtrip(self):
        for limit in (0, 201, -1):
            with self.assertRaises(ValueError):
                await self.repository.list_staff_audit(limit=limit)
        await self.repository.record_staff_action(
            ADMIN, "custom", None, {"текст": "значение", "n": 1}
        )
        entry = (await self.repository.list_staff_audit(limit=1))[0]
        self.assertEqual(entry.details, {"текст": "значение", "n": 1})

    async def test_profile_edit_requires_admin_and_logs_field_names_only(self):
        await self.repository.get_or_create_profile(USER, "Анна")
        with self.assertRaises(PermissionError):
            await self.repository.admin_update_profile(USER, USER, user_name="Хакер")
        self.assertTrue(
            await self.repository.admin_update_profile(USER, ADMIN, phone="89001234567")
        )
        self.assertFalse(
            await self.repository.admin_update_profile(999999, ADMIN, user_name="x")
        )
        profile = await self.repository.get_profile(USER)
        self.assertEqual((profile.user_name, profile.phone), ("Анна", "89001234567"))
        entry = next(
            e
            for e in await self.repository.list_staff_audit()
            if e.action == "profile_updated"
        )
        self.assertEqual(entry.details, {"fields": ["phone"]})
        self.assertNotIn("89001234567", str(entry.details))


if __name__ == "__main__":
    unittest.main()
