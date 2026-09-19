# Despliegue en Railway

Un proyecto Railway con dos entornos (`staging`, `production`). Este repo
aporta tres servicios, todos construidos con el mismo `Dockerfile`; en cada
servicio, **Settings → Config-as-code** apunta al archivo correspondiente:

| Servicio | Archivo | Rol |
|---|---|---|
| `backend-web` | `railway/web.json` | Daphne (HTTP + WebSocket). Pre-deploy: migraciones y `bootstrap_platform`. Healthcheck `/healthz`. |
| `celery-worker` | `railway/worker.json` | Tareas asíncronas (exportaciones, correos). |
| `celery-beat` | `railway/beat.json` | Tareas periódicas. **Siempre 1 réplica.** |

Además, en el mismo proyecto: **PostgreSQL** y **Redis** (plantillas de
Railway) y un **Bucket** S3-compatible. El frontend (`FivuzaFrontend`) es
otro servicio con su propio `railway.json`, y es el único con dominio
público: nginx reenvía `/api` y `/ws` a `backend-web` por la red privada.

## Variables (compartidas por los tres servicios)

| Variable | Valor |
|---|---|
| `DEBUG` | `False` |
| `SECRET_KEY` | generada (`python -c "import secrets;print(secrets.token_urlsafe(50))"`) |
| `ALLOWED_HOSTS` | `.fivuza.com,.railway.internal` (staging: `.staging.fivuza.com,.railway.internal`) |
| `CSRF_TRUSTED_ORIGINS` | `https://*.fivuza.com` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` / `CELERY_BROKER_URL` | `${{Redis.REDIS_URL}}` |
| `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_REGION` | credenciales del Bucket |
| `AWS_S3_ADDRESSING_STYLE` | `path` si el bucket no admite virtual-host |
| `MEDIA_PUBLIC_BASE_URL` | base pública para imágenes (vacío = URL del endpoint) |
| `FRONTEND_SCHEME` / `FRONTEND_PORT` | `https` / vacío |
| `EMAIL_BACKEND`, `DEFAULT_FROM_EMAIL` | proveedor de correo por API HTTP (Railway bloquea SMTP saliente en planes bajos) |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT` | proyecto de Sentry / `staging` o `production` |
| `PUBLIC_DOMAINS` | dominios del panel interno, ej. `admin.fivuza.com` |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | primer SUPER_ADMIN; borrar la contraseña tras el primer deploy |

## Dominios

- `*.fivuza.com` (wildcard) y `admin.fivuza.com` → servicio **frontend**.
  Railway pide un CNAME y un registro TXT de verificación por dominio.
- Cada negocio nuevo se registra desde el panel interno con su subdominio
  (`<negocio>.fivuza.com`); no hace falta tocar DNS gracias al wildcard.
- `backend-web` no necesita dominio público.

## Primer despliegue

1. Crear Postgres, Redis y Bucket; cargar las variables.
2. Desplegar `backend-web`: el pre-deploy migra y corre `bootstrap_platform`.
3. Desplegar `celery-worker`, `celery-beat` y el frontend.
4. Verificar `https://admin.fivuza.com/api/v1/health/` y el login del panel.
5. Borrar `BOOTSTRAP_ADMIN_PASSWORD`.

Rollback: en Railway, *Deployments → Redeploy* del despliegue anterior. Si
el deploy incluía una migración no reversible, restaurar primero el backup
de Postgres (ver runbook en `Fivuza-Docs`).
