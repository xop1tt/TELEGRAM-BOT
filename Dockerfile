# Образ закреплён по digest: сборка не подхватит молча другой образ под тем
# же тегом. Обновление — осознанно: docker buildx imagetools inspect <тег>.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BOT_LOG_DIR=/var/log/flamenco

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && python -m pip install --no-cache-dir --no-deps . \
    && mkdir -p "$BOT_LOG_DIR" \
    && chown -R 10001:10001 "$BOT_LOG_DIR"

USER 10001:10001

CMD ["python", "-m", "flamenco_bot"]
