# 💃 Flamenco Studio — Telegram-бот и веб-API

[![Tests](https://github.com/xop1tt/TELEGRAM-BOT/actions/workflows/ci.yml/badge.svg)](https://github.com/xop1tt/TELEGRAM-BOT/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.31-149ECA)](https://docs.aiogram.dev/)

Backend студии фламенко: **Telegram-бот** (aiogram) и **веб-API** (FastAPI) —
два интерфейса к одной бизнес-логике и одной базе PostgreSQL. Участники
записываются на занятия, покупают абонементы и пишут в поддержку; сотрудники
ведут расписание, участников, финансы и рассылки — всё прямо в Telegram. Тот же
backend обслуживает [сайт студии](https://github.com/xop1tt/WEBSITE).

## Возможности

**Участникам**

- 📅 расписание на 7 дней с листанием недель, запись в один шаг, отмена, лист ожидания;
- 🎟 абонементы со сроком действия, баланс и история операций;
- 💳 покупка абонементов через ЮKassa, история покупок;
- 🔔 уведомления: запись, отмена, перенос, напоминание, малый остаток, сгорание;
- 💬 обращения в поддержку, профиль, вход на сайт через бота.

**Сотрудникам** (`/admin`, роли администратор и преподаватель)

- 🗓 расписание: создание, серии на 12 недель, копирование недели, перенос, отмена;
- 👥 участники: карточка, выдача абонементов, корректировка баланса, запись и отмена за участника, заметки;
- 📣 рассылки по сегментам; 📨 поддержка; 💰 финансы, журнал, сверка;
- ⚙️ настройки: каталог абонементов и направлений, правила записи, сотрудники, сбои доставки.

## Как устроено

```text
Telegram ── polling ──► бот ─┐
                             ├─► services ─► PostgreSQL (Supabase, схема flamenco)
сайт / iOS ── HTTPS ──► API ─┘
```

Вся бизнес-логика — в `src/flamenco_bot/services`; обработчики бота и маршруты
API только принимают ввод и вызывают сервисы. Схему БД меняет отдельный
репозиторий [DATABASE](https://github.com/xop1tt/DATABASE); бот при старте
проверяет версию схемы.

| Репозиторий | Что внутри |
|---|---|
| **TELEGRAM-BOT** (этот) | бот, веб-API, бизнес-логика |
| [WEBSITE](https://github.com/xop1tt/WEBSITE) | сайт на Next.js |
| [DATABASE](https://github.com/xop1tt/DATABASE) | миграции и резервные копии |

## Быстрый запуск

Нужны Python 3.10+, токен бота от [@BotFather](https://t.me/BotFather) и
PostgreSQL с применёнными миграциями из `DATABASE`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
cp env.example .env       # BOT_TOKEN, DATABASE_URL, DATABASE_SSL_MODE=disable (локально)
./run.sh                  # API + бот; ./run.sh api — только API
```

Всё вместе с сайтом и локальной базой поднимает `./start-mac.command` в общей
папке проекта. Первого администратора назначают командой
`PYTHONPATH=src python -m flamenco_bot.api.manage grant-admin --telegram-id <ID>`.

## Проверки

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
TEST_DATABASE_URL=postgresql://… .venv/bin/python -m unittest discover -s tests
```

Тесты используют настоящую PostgreSQL; без `TEST_DATABASE_URL` большинство
пропускается. CI запускает всё на Python 3.10, 3.11 и 3.13.

## Документация

Подробная вики: **<https://github.com/xop1tt/TELEGRAM-BOT/wiki>**

- [Возможности участника](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Возможности-участника) · [Админ-панель в боте](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Админ-панель-в-боте)
- [Запись и отмена занятий](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Запись-и-отмена-занятий) · [Абонементы и срок действия](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Абонементы-и-срок-действия) · [Платежи ЮKassa](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Платежи-ЮKassa)
- [Веб-API](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Веб-API) · [Архитектура](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Архитектура) · [Конфигурация](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Конфигурация)
- [Развёртывание и обновление](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Развёртывание-и-обновление) · [Эксплуатация](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Эксплуатация) · [Частые проблемы](https://github.com/xop1tt/TELEGRAM-BOT/wiki/Частые-проблемы)

В репозитории: [`docs/api.md`](./docs/api.md) — справочник веб-API,
[`docs/deployment.md`](./docs/deployment.md) — размещение,
[`docs/operations.md`](./docs/operations.md) — эксплуатационные процедуры,
[`CLAUDE.md`](./CLAUDE.md) — правила разработки.

## Лицензия

Файла лицензии пока нет: публичная доступность исходников не даёт права их
повторного использования или изменения; все права сохраняются за
правообладателем.
