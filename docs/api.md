# Web API для сайта

Сайт (Next.js, отдельный репозиторий) работает с теми же данными и правилами,
что и Telegram-бот, только через этот API: своей бизнес-логики и доступа к
PostgreSQL у сайта нет. Все пути — под `/api`; в production сайт проксирует их
к контейнеру `api` (см. `compose.yaml`, `docs/deployment.md`).

Интерактивная схема: `GET /docs` (Swagger) и `GET /openapi.json` у запущенного
API.

## Общие правила

- **Сессия** — httpOnly-cookie `session` (Secure, SameSite=Lax, 7 дней).
  Сайт её не читает и не хранит: браузер отправляет её сам. Запросы из
  браузера — с `credentials: "include"` (или через прокси того же домена).
- **Время** — ISO 8601 в UTC (`2026-10-08T16:00:00Z`). Показывать в поясе
  студии `STUDIO_TIMEZONE` (по умолчанию `Europe/Moscow`) — как бот.
- **Деньги** — `amount_minor` в копейках, цены каталога — `price_rub`.
- **Ошибки** — JSON `{"detail": "текст для пользователя"}`:

  | Код | Когда |
  |---|---|
  | 401 | нет входа или сессия истекла |
  | 404 | объект не найден (или чужой — сайт не узнает, что он существует) |
  | 409 | конфликт бизнес-правил: нет мест, нет занятий на балансе, поздно отменять, Telegram не привязан, Telegram уже у другого аккаунта |
  | 422 | неверные данные запроса (в том числе id больше 2⁶³−1) |
  | 429 | слишком много попыток входа |
  | 503 | БД или ЮKassa временно недоступны (`Database temporarily unavailable`) |

- **Telegram обязателен для кабинета.** Баланс, записи, абонементы и
  поддержка привязаны к Telegram-профилю участника (тот же, что в боте).
  Вошедший по email без привязанного Telegram получает 409 с текстом «Привяжите
  Telegram к аккаунту…» — покажите кнопку привязки (см. ниже).

## Вход

Один участник — один аккаунт: аккаунт сайта ссылается на Telegram-профиль
бота и не дублирует его данные.

### Вход через бота (рекомендуется, без SMS и без виджета)

Работает на любом домене и на телефоне.

1. `POST /api/auth/telegram/connect` с телом `{"purpose": "login"}` →
   `201 {"deep_link": "https://t.me/<бот>?start=c_…", "expires_at": "…",
   "poll_interval_seconds": 2}`. Браузер получает httpOnly-cookie
   `tg_connect` (только для `/api/auth/telegram/connect*`).
2. Откройте `deep_link` (новая вкладка или приложение Telegram). Участник
   нажимает в боте «✅ Войти на сайт».
3. Опрашивайте `POST /api/auth/telegram/connect/complete` раз в
   `poll_interval_seconds` до 10 минут. Ответ `200` с полем `status`:
   - `pending` — ещё не подтвердили, опрашивайте дальше;
   - `completed` — вход выполнен, установлена cookie `session`, в `user` —
     текущий пользователь (выдаётся **один раз**);
   - `rejected` — отказ в боте (или Telegram уже у другого аккаунта с email);
     `expired` — ссылка устарела; `used` — запрос уже использован. Опрос
     прекратить.

   `400` — в браузере нет активного запроса (начните заново), `404` — запрос
   не найден.

Безопасность: Telegram ID берёт бот из апдейта Telegram, а не из сайта;
сессию получает только браузер с секретом из `tg_connect`; токен и секрет
одноразовые, в БД — только их SHA-256. Не логируйте и не показывайте
`deep_link` третьим лицам: подтвердивший его в своём Telegram войдёт от своего
имени.

### Привязка Telegram к аккаунту с email

Тот же поток с `{"purpose": "link"}` (нужна сессия, иначе 401). После
`completed` у текущего аккаунта появляется `telegram_id`. Если этот Telegram
уже использовался для входа на сайт (аккаунт без email), аккаунты
объединяются; если он привязан к другому аккаунту **с email** — бот покажет
отказ, а опрос вернёт `rejected`.

### Telegram Login Widget и email (как раньше)

- `POST /api/auth/telegram` — данные виджета (подпись проверяет сервер).
- `POST /api/auth/me/telegram` — привязать Telegram виджетом (тоже объединяет
  аккаунт без email); `DELETE /api/auth/me/telegram` — отвязать (нельзя, если
  это единственный способ входа).
- `POST /api/auth/register`, `POST /api/auth/login` — email и пароль.
- `POST /api/auth/logout` — сессия отзывается на сервере сразу.

### Текущий пользователь

`GET /api/auth/me` → `{"id", "email", "telegram_id", "display_name",
"created_at"}`; `401` — не вошёл.

## Главная кабинета одним запросом

`GET /api/users/me/overview`:

```json
{
  "profile": {"telegram_id": 1001, "user_name": "Анна", "phone": null,
              "lesson_credits": 6, "registered_at": "…", "is_admin": false},
  "packages": {"balance": 6, "unallocated": 0,
               "next_source_title": "Абонемент на 8 занятий",
               "packages": [{"kind": "payment", "id": 12, "title": "…",
                             "lessons": 8, "remaining": 6, "status": "active",
                             "status_label": "действует", "acquired_at": "…",
                             "amount_minor": 680000}]},
  "upcoming": [ /* как в /api/bookings/me */ ],
  "unread_notifications": 2,
  "notification_settings": {"reminders": true, "low_balance": true}
}
```

Профиль отдельно: `GET /api/users/me/profile`, имя —
`PATCH /api/users/me/profile` `{"user_name": "…"}`. Телефон меняется только в
боте (подтверждение контактом Telegram).

## Расписание и запись

- `GET /api/schedule[?class_key=beginner&available_only=true]` — будущие
  занятия (отменённые студией не показываются). Поля: `id`, `class_key`,
  `class_label`, `starts_at`, `capacity`, `remaining`, `status`
  (`open`/`closed`), `bookable`, `rescheduled`.
- `GET /api/schedule/{slot_id}` — карточка занятия в любом статусе, в том
  числе `cancelled` с `cancel_reason`; `404` — нет такого.
- `GET /api/classes` — направления; `GET /api/bookings/rules` — окно отмены и
  пауза повторной записи (числа для текста правил, не хардкодьте их).
- `POST /api/bookings` `{"slot_id": 5}` → `201` с `balance` после списания и
  `source` (абонемент, с которого списано занятие; `null` — с занятий вне
  абонементов). Повторный запрос на то же занятие — `already_booked: true`,
  без второго списания. `409` — нет мест/закрыто/началось, нет занятий на
  балансе, пауза после отмены.
- `DELETE /api/bookings/{slot_id}` → `204` (повтор — тоже `204`, без второго
  возврата). `409` — поздно отменять, `404` — записи нет.
- `GET /api/bookings/me?scope=all|upcoming|past` — записи: `booking_status`,
  `slot_status`, `status` (`upcoming`, `attended`, `cancelled_by_user`,
  `cancelled_by_studio`) и `status_label`, `can_cancel`, `cancellable_until`,
  `previous_starts_at` (время до переноса), `slot_cancel_reason`.

Правила (проверяет только сервер, сайт лишь показывает): запись списывает 1
занятие — сначала занятия вне абонементов, затем самый ранний абонемент;
отмена не позднее чем за 24 часа возвращает занятие в тот же абонемент;
повторная запись на то же занятие после отмены — через 12 часов. Если студия
перенесла занятие после записи, отменить можно до нового начала
(`cancellable_until` = `starts_at`). Отмена студией возвращает занятие
автоматически.

Запись и отмена с сайта присылают участнику уведомление в Telegram.

## Абонементы, покупки, баланс

- `GET /api/packages` — каталог (цены — только отсюда).
- `GET /api/packages/me` — мои абонементы: купленные (`kind: payment`) и
  выданные студией (`grant`); `remaining`, `status` (`active`, `used`,
  `refund_pending`, `refunded`, `revoked`). Сумма `remaining` + `unallocated`
  всегда равна `balance`.
- `POST /api/payments/checkout` `{"package_key": "pack_4"}` →
  `confirmation_url` ЮKassa (`503`, пока не задан `WEB_YOOKASSA_RETURN_URL`).
- `GET /api/payments/{id}/check` — проверить оплату после возврата с ЮKassa
  (зачисление — только по статусу от ЮKassa, ровно один раз).
- `GET /api/payments/me` — история покупок.

## История

- `GET /api/history/operations?limit=20[&before_id=…]` — движения баланса из
  ledger, новые сверху: `operation` (`purchase`, `lesson_use`,
  `booking_refund`, `studio_cancellation`, `manual_credit`, `manual_debit`,
  `package_grant`, `package_revoke`, `payment_refund`, …), `label`, `delta`,
  `reason` (только для операций студии), `package_title`, `class_label`,
  `starts_at`. Следующая страница — `before_id = next_before_id`.
- `GET /api/history/classes?limit=20&offset=0` — история занятий (поля как у
  `/api/bookings/me`), следующая страница — `next_offset`.

## Уведомления

То, что бот присылает в Telegram, видно и на сайте.

- `GET /api/notifications/me?limit=20[&unread_only=true&before_id=…]` →
  `{"items": [{"id", "kind", "title", "body", "created_at", "read"}],
  "unread_count", "next_before_id"}`.
- `GET /api/notifications/me/unread-count` — для значка.
- `POST /api/notifications/me/read` `{"ids": [1, 2]}` или `{}` (все) →
  `{"updated": N}`; чужие id игнорируются.
- `GET` / `PATCH /api/users/me/notification-settings` `{"reminders": false,
  "low_balance": true}`. Об отмене и переносе занятия студией и изменении
  баланса участник узнаёт всегда.

Типы (`kind`): `booking_confirmed`, `booking_cancelled`, `slot_cancelled`,
`slot_rescheduled`, `lesson_reminder`, `low_balance`, `package_granted`,
`package_revoked`, `credits_adjusted`, `package_expiring`, `credits_expired`,
`waitlist_seat`, `announcement`, `maintenance_*` (см. «Техническое
обслуживание сайта»).

## Техническое обслуживание сайта

Состояние хранится в `site_maintenance` (миграция 021 DATABASE) — один
источник для сайта, API и бота. Время — ISO 8601 со смещением (в БД — UTC),
людям показывается в поясе студии `STUDIO_TIMEZONE`.

- `GET /api/maintenance` — публично, `Cache-Control: no-store` →
  `{"mode": "normal" | "scheduled" | "active", "title", "description",
  "starts_at", "ends_at", "server_time", "retry_after"}`. При `normal` тексты
  и время — `null`. Окно с наступившим началом отдаётся как `active` сразу,
  не дожидаясь фоновой задачи; наступление `ends_at` обслуживание не
  выключает.

Только администратору (роль `admin` в `staff_members`; иначе `401`/`403`):

- `GET /api/admin/maintenance` — текущее окно (с планом предупреждений,
  журналом и статистикой доставки), история, значения по умолчанию, размер
  аудитории.
- `GET /api/admin/maintenance/{id}` — то же по одному окну.
- `POST /api/admin/maintenance` `{"title", "description", "starts_at",
  "ends_at"?, "warning_minutes": [1440, 60, 10], "notify_users": true,
  "audience": "all" | "staff", "start_now": false}` → `201`. Время без пояса —
  `422`; начало в прошлом, окончание раньше начала — `422`; уже есть
  незавершённое окно — `409`. `start_now` включает обслуживание сразу.
- `PATCH /api/admin/maintenance/{id}` — только переданные поля
  (`ends_at: null` — окончание неизвестно). Во время работ — только тексты и
  окончание, иначе `409`. Изменение времени — перенос: получившим
  предупреждение уходит уведомление о переносе.
- `POST /api/admin/maintenance/{id}/start` — включить запланированное сейчас.
- `POST /api/admin/maintenance/{id}/complete` — работы завершены (только
  идущие; `409` иначе). Только после этого уходит уведомление о завершении.
- `POST /api/admin/maintenance/{id}/cancel` — отменить запланированное
  (начавшееся — только завершить, `409`).
- `POST /api/admin/maintenance/preview` — тело как у создания → тексты всех
  уведомлений, план предупреждений и число получателей. Ничего не
  сохраняет и не отправляет (тестовый режим).

`audience: "staff"` — тестовый режим: уведомления получают только
сотрудники. Уведомления (`kind`): `maintenance_warning`,
`maintenance_started`, `maintenance_rescheduled`, `maintenance_cancelled`,
`maintenance_completed` — в общей ленте и в Telegram.

## Поддержка

- `POST /api/support` `{"body": "…"}` — сообщение (новое обращение или в
  открытое); `429` — не больше 5 сообщений за 5 минут.
- `GET /api/support/me` — мои обращения со статусом и последним сообщением.
- `GET /api/support/me/{ticket_id}` — переписка: `messages[]` с
  `sender_role` (`user` — участник, `admin` — студия). Чужое обращение — 404.

Ответ студии приходит участнику и в Telegram.

## Здоровье

`GET /api/health` → `{"backend": "postgres", …}`; `503`, если БД недоступна.
