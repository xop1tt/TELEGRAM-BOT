# Тестирование бота и веб-API

## Главное: почему тесты «пропускаются»

Большинство тестов бота работают на **настоящем PostgreSQL** (схема — миграции
репозитория DATABASE). Без `TEST_DATABASE_URL` они пропускаются: из 783 тестов
выполняется 359, а покрытие (строки+ветви) падает с ~90% до ~52%. Это не «отключённые» тесты —
им просто нужна база (единственная причина всех пропусков). **Запускайте с базой.**

| Запуск | Тестов | Пропущено | Строки | Ветви |
|---|---|---|---|---|
| без `TEST_DATABASE_URL` | 783 | 424 | 56.8% | 31.9% |
| с `TEST_DATABASE_URL` + `DATABASE_DIR` | 783 | 0 | 91.9% | 79.1% |

Самый простой способ — `scripts/test-with-postgres.sh`: поднимает одноразовый
контейнер `postgres:17`, прогоняет всё с покрытием и удаляет контейнер
(работает и с готовым `TEST_DATABASE_URL`).

## Окружение

```bash
# Одноразовая PostgreSQL (любая; ни в коем случае не production и не Supabase):
docker run --rm -d --name flamenco-test-pg -p 55432:5432 \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test postgres:17
#   или Postgres.app / локальный сервер: createdb test

export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/test
export DATABASE_DIR="../DATABASE"      # репозиторий со схемой (migrate.py)
export BOT_TOKEN=123456:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   # любой, сеть не используется
export ENV=development

python -m pip install --require-hashes -r requirements-dev.txt
```

Каждый тест получает свежую схему (`tests/pg.py`): миграции применяются один
раз на процесс, перед тестом таблицы очищаются, справочные строки
восстанавливаются. Осиротевшие схемы прошлых прогонов удаляются автоматически.

## Команды

```bash
# как в CI (unittest) — с покрытием
python -m coverage run -m unittest discover -s tests -v
python -m coverage report --skip-empty --fail-under=85

# одной командой с одноразовой БД в Docker:
scripts/test-with-postgres.sh                 # всё + покрытие
scripts/test-with-postgres.sh tests/unit -q   # любые аргументы pytest

# быстрее и читаемее локально (pytest, есть в requirements-dev)
python -m pytest tests -q
python -m pytest tests/unit/test_admin_log_paging.py -q       # один файл
python -m pytest tests -q -k "booking and not cancel"         # по имени

ruff check src tests && ruff format --check src tests         # линтер, как в CI
```

Покрытие настроено в `pyproject.toml` (`[tool.coverage.*]`, ветви включены, файлы
из отчёта **не исключаются**).

## Структура

| Каталог | Что внутри |
|---|---|
| `tests/unit` | обработчики, клавиатуры, сервисы, API (`TestClient`) и репозиторий на PostgreSQL; быстрые тесты без БД (форматы, правила, конфиг, платёжный клиент) |
| `tests/integration` | глубокие сценарии БД: ledger, конкурентные операции, схема, оплата и возвраты |
| `tests/e2e` | `serve_api.py` — настоящий API с сидом для сквозных тестов сайта (репозиторий WEBSITE) и тест его запуска |
| `tests/support.py`, `tests/pg.py` | `FakeMessage/FakeRepository/FakeState` и PostgreSQL-фикстуры |

## Что проверяется (критичное)

* **Бронирование**: вместимость, закрытое/начавшееся занятие, пауза после своей
  отмены, идемпотентность записи, списание ровно 1 занятия, откат при нехватке
  баланса, конкурентные записи (двойного списания и овербукинга нет).
* **Отмена**: повторная отмена не возвращает занятие второй раз, окно отмены,
  отмена студией/админом, возврат в тот же абонемент.
* **Деньги**: корректировки баланса с ключом идемпотентности, сверка с ledger,
  оплата (`complete` один раз), возвраты (все статусы), клиент ЮKassa
  (копейки↔рубли, `Idempotence-Key`, HTTP/сеть/битый JSON — без сети).
* **Права**: каждый админский callback отказывает не-админу, преподаватель видит
  только свои занятия, смена ролей и защита последнего администратора
  (в том числе при гонке), серверные команды `api.manage`.
* **Админ-панель**: прокрутка «Последние операции» и «Журнал операций» (по 7),
  «Состояние бота», сверка баланса, вместимость, профиль участника.
* **Фоновые процессы**: supervisor (keep-alive, зависший heartbeat), уведомления,
  сверка платежей, срок действия абонементов, обслуживание сайта.

## Что мокается

Telegram (`FakeMessage/FakeCallback`, `AsyncMock` для `bot.send_message`),
ЮKassa (`urlopen` или `FakeGateway`) и время там, где нужен срок. **БД не
мокается**: правила, ограничения и транзакции проверяет сам PostgreSQL.
Проверяйте итоговое состояние (баланс, ledger, число мест), а не число вызовов.

## Сквозные тесты сайта

`python tests/e2e/serve_api.py [порт]` поднимает API на одноразовой схеме и печатает
`READY {json}` (порт, пароль, id занятий). Его использует WEBSITE (`npm test`,
см. `WEBSITE/TESTING.md`). `tests/e2e/test_serve_api.py` проверяет сам сервер.

## Известные ограничения

* Покрыты слабее 80%: `handlers/admin_panel.py` (77%, FSM-ветки редких форм),
  `handlers/admin_broadcasts.py` (79%). Функции (function coverage) `coverage.py` не измеряет.
* Прямой `import flamenco_bot.keyboards.user.purchases` без предварительного
  `import flamenco_bot.handlers` падает из-за циклического импорта (в приложении
  порядок всегда верный) — в тестах учтено.
* Версия PostgreSQL локально может отличаться от CI (там 17); синтаксис
  проверен на 17 и 18.
* Тесты не обращаются к Telegram API и ЮKassa — реальные интеграции проверяются
  вручную на тестовом боте/магазине.

## Как добавить тест

1. Нужна БД → наследуйтесь от `tests.pg.PostgresTestCase` (`self.repository`);
   нужен только обработчик → `FakeRepository` из `tests/support.py`.
2. Права и отказ: проверьте, что при отказе **ничего не изменилось** (баланс, ledger).
3. Повторы и гонки: `asyncio.gather` на настоящей БД, как в `tests/integration`.
4. Проверьте, что тест падает при поломке кода (временно испортите условие).
