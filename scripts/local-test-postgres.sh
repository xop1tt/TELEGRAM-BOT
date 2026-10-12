#!/usr/bin/env bash
# Одноразовая локальная PostgreSQL для тестов без Docker (нужен Postgres.app или
# любая установка с initdb/pg_ctl). Отдельный каталог данных, порт 55432, только
# 127.0.0.1, без пароля — не база разработки (5432) и не production.
#
#   scripts/local-test-postgres.sh start | stop | status | reset
#
# Адрес для тестов: postgresql://test@127.0.0.1:55432/test (.vscode/test.env).
set -euo pipefail
PORT="${TEST_PG_PORT:-55432}"
DATA="${TEST_PG_DATA:-$HOME/.flamenco-test-postgres}"
SOCK="/tmp/flamenco-pg-$PORT"
BIN="${PG_BIN:-}"
if [ -z "$BIN" ]; then
  for candidate in /Applications/Postgres.app/Contents/Versions/*/bin /opt/homebrew/opt/postgresql*/bin /usr/lib/postgresql/*/bin; do
    [ -x "$candidate/pg_ctl" ] && BIN="$candidate"
  done
fi
[ -n "$BIN" ] || { echo "Не найден pg_ctl (задайте PG_BIN)" >&2; exit 2; }
case "${1:-status}" in
  start)
    mkdir -p "$SOCK"
    [ -d "$DATA" ] || "$BIN/initdb" -D "$DATA" -U test --auth=trust -E UTF8 >/dev/null
    "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1 || \
      "$BIN/pg_ctl" -D "$DATA" -o "-p $PORT -k $SOCK -c listen_addresses=127.0.0.1" -l "$DATA/server.log" -w start >/dev/null
    "$BIN/psql" -h 127.0.0.1 -p "$PORT" -U test -d postgres -Atc "select 1 from pg_database where datname='test'" | grep -q 1 || \
      "$BIN/createdb" -h 127.0.0.1 -p "$PORT" -U test test
    echo "готово: postgresql://test@127.0.0.1:$PORT/test" ;;
  stop) "$BIN/pg_ctl" -D "$DATA" -m fast stop ;;
  status) "$BIN/pg_ctl" -D "$DATA" status ;;
  reset) "$BIN/pg_ctl" -D "$DATA" -m fast stop 2>/dev/null || true; rm -rf "$DATA"; echo "каталог данных удалён" ;;
  *) echo "start|stop|status|reset" >&2; exit 2 ;;
esac
