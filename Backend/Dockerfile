FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_EMBEDDINGS=false
COPY requirements-runtime.lock requirements-embeddings.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-runtime.lock \
    && if [ "$INSTALL_EMBEDDINGS" = "true" ]; then pip install --no-cache-dir --require-hashes -r requirements-embeddings.lock; fi

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1,::1}\" --no-server-header"]