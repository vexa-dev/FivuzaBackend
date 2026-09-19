# Imagen de produccion (Railway): dependencias en una etapa aparte para que
# la imagen final no lleve compiladores, y el proceso no corre como root.
FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 fivuza

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=fivuza:fivuza . /app/

# collectstatic no necesita secretos reales ni BD, solo que settings cargue.
RUN SECRET_KEY=solo-para-collectstatic DEBUG=False \
    DATABASE_URL=postgres://build:build@localhost:5432/build \
    python manage.py collectstatic --noinput

USER fivuza
EXPOSE 8000

# Railway inyecta PORT; en local queda 8000. Worker y beat sobrescriben el
# comando (ver railway/ y docker-compose.yml).
CMD ["sh", "-c", "daphne -b 0.0.0.0 -p ${PORT} config.asgi:application"]
