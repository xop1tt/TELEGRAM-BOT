#!/usr/bin/env bash
# Полный набор тестов с одноразовой PostgreSQL в Docker — без неё ~400 тестов
# пропускаются (см. TESTING.md). Контейнер удаляется по завершении; данные
# разовые, production и Supabase не затрагиваются.
#
#   scripts/test-with-postgres.sh [аргументы pytest/unittest...]
#
# Если TEST_DATABASE_URL уже задан, используется он (контейнер не создаётся).
set -euo pipefail
cd "$(dirname "$0")/.."

export DATABASE_DIR="${DATABASE_DIR:-../DATABASE}"
export BOT_TOKEN="${BOT_TOKEN:-123456:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx}"
export ENV="${ENV:-development}"

if [ -z "${TEST_DATABASE_URL:-}" ]; then
  command -v docker >/dev/null || { echo "Нужен Docker или готовый TEST_DATABASE_URL" >&2; exit 2; }
  name="flamenco-test-pg-$$"
  docker run --rm -d --name "$name" -p 127.0.0.1::5432 \
    -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test postgres:17 >/dev/null
  trap 'docker stop "$name" >/dev/null 2>&1 || true' EXIT
  port="$(docker port "$name" 5432/tcp | head -n1 | sed 's/.*://')"
  for _ in $(seq 1 60); do
    docker exec "$name" pg_isready -U test -d test >/dev/null 2>&1 && break
    sleep 1
  done
  export TEST_DATABASE_URL="postgresql://test:test@127.0.0.1:${port}/test"
fi

PY="${PYTHON:-python}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
if [ "$#" -gt 0 ]; then
  exec "$PY" -m pytest "$@"
fi
"$PY" -m coverage run -m unittest discover -s tests
"$PY" -m coverage report --skip-empty
