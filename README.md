# Fivuza — Backend

[![Backend CI](https://github.com/vexa-dev/FivuzaBackend/actions/workflows/ci.yml/badge.svg)](https://github.com/vexa-dev/FivuzaBackend/actions/workflows/ci.yml)

API REST del **ERP SaaS multi-tenant** de Fivuza, orientado a pequeños y medianos negocios (bodegas, gimnasios, tiendas de retail). Construido con Django + Django REST Framework, con aislamiento de datos por esquema de PostgreSQL (schema-per-tenant vía [django-tenants](https://django-tenants.readthedocs.io/)).

> Proyecto privado. Repositorios complementarios: [FivuzaFrontend](https://github.com/vexa-dev/FivuzaFrontend) (React + Vite) y [Fivuza-Docs](https://github.com/vexa-dev/Fivuza-Docs) (documentación; el contrato vigente está en `FIVUZA_Estado_Implementado_2026-09.md`).

---

## Tabla de contenidos

- [Stack técnico](#stack-técnico)
- [Arquitectura](#arquitectura)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Primeros pasos](#primeros-pasos)
- [Variables de entorno](#variables-de-entorno)
- [Comandos útiles](#comandos-útiles)
- [Pruebas y linting](#pruebas-y-linting)
- [Documentación de la API](#documentación-de-la-api)
- [Despliegue](#despliegue)
- [Convenciones de contribución](#convenciones-de-contribución)
- [Estado del proyecto](#estado-del-proyecto)

---

## Stack técnico

| Categoría | Tecnología |
|---|---|
| Lenguaje / Framework | Python 3.12, Django 5.2 LTS, Django REST Framework |
| Multi-tenancy | django-tenants (aislamiento por esquema de PostgreSQL) |
| Base de datos | PostgreSQL 15 |
| Autenticación | JWT (`djangorestframework-simplejwt`), dos flujos separados (`platform_staff` / `tenant.users`); refresh en cookie HttpOnly, access solo en memoria |
| Async / tiempo real | Django Channels + Daphne (ASGI), Celery + Redis |
| Documentación de API | drf-spectacular (OpenAPI / Swagger / ReDoc) |
| Archivos | Bucket S3 o S3-compatible vía `core/storage.py` |
| Observabilidad | Sentry, logs a stdout |
| Linting / formato | [Ruff](https://docs.astral.sh/ruff/) |
| CI | GitHub Actions (lint → test con cobertura → build, compatibilidad de OpenAPI) |
| Despliegue | Docker; [Railway](railway/README.md) en staging y producción |

## Arquitectura

Cada negocio cliente (tenant) tiene su propio esquema en PostgreSQL. El esquema `public` centraliza la plataforma (tenants, planes, suscripciones, equipo interno de Fivuza).

| App | Contiene | Esquema |
|---|---|---|
| `core` | Tenants, planes, suscripciones, equipo interno (`platform_staff`), autorización por almacén, storage, middleware | `public` (compartida) |
| `usuarios` | RBAC (roles/permisos), autenticación de `tenant.users`, RR. HH., respaldo de datos | Por tenant |
| `inventario` | Catálogo, variantes, marcas, stock, Kardex, traslados, compras, impuestos, etiquetas | Por tenant |
| `ventas` | POS, pagos, devoluciones, crédito/saldo, caja, promociones, apartados, cotizaciones, sync offline | Por tenant |
| `dashboard` | Métricas de solo lectura (vistas materializadas + WebSocket) | Por tenant |
| `gimnasio` | Membresías, clases, grupos, control de acceso | Por tenant |

Cada app de negocio sigue `ViewSet → Serializer → Service → Model`. La lógica de escritura vive en servicios; las consultas reutilizables, en `selectors.py`.

Puntos transversales:

- **Autorización por almacén:** `WarehouseAccessService` (`core/warehouse_access.py`) es la única fuente de alcance.
- **Rutas:** un solo URLconf, pero `SchemaRouteGuardMiddleware` responde 404 a rutas de negocio en el dominio `public` y a rutas de plataforma en dominios de tenant.
- **Zona horaria:** `America/Lima`. Las ventas guardan `occurred_at` (hora real, también para ventas offline).
- **Contrato de API:** paginación DRF y errores `{"error": {code, message, details}}` activos por defecto.
- **Tareas periódicas:** `run_per_tenant` (`core/tenant_tasks.py`) aísla cada tenant: si uno falla, la tarea sigue con los demás.

## Estructura del proyecto

```
FivuzaBackend/
├── config/          # settings, urls, asgi/wsgi, celery
├── core/            # esquema public + piezas transversales (storage, middleware, acceso por almacén)
├── usuarios/        # RBAC + RR. HH. + respaldo (esquema tenant)
├── inventario/      # catálogo, stock, compras, impuestos (esquema tenant)
├── ventas/          # POS, caja, devoluciones, créditos, apartados, cotizaciones (esquema tenant)
├── dashboard/       # métricas agregadas (esquema tenant)
├── gimnasio/        # vertical de gimnasios (esquema tenant)
├── railway/         # config-as-code de Railway + guía de despliegue
└── scripts/         # utilidades de CI (diff de OpenAPI)
```

## Primeros pasos

### Requisitos

- Docker y Docker Compose
- (Opcional) Python 3.12 local

### 1. Clonar y configurar variables de entorno

```bash
git clone https://github.com/vexa-dev/FivuzaBackend.git
cd FivuzaBackend
cp .env.example .env
```

Con `DEBUG=True`, `SECRET_KEY` puede quedar vacía en desarrollo. Con `DEBUG=False` es obligatoria: el backend no arranca sin ella.

### 2. Levantar el entorno

```bash
docker compose up -d
```

Levanta `db`, `redis`, `web` (Daphne en `:8000`), `celery_worker`, `celery_beat` y `frontend` (Vite en `:5173`, si `FivuzaFrontend` está clonado como carpeta hermana). La imagen de desarrollo instala `requirements-dev.txt`.

### 3. Migraciones y tenants de prueba

```bash
docker compose exec web python manage.py migrate_schemas --shared
docker compose exec web python poc_tenant.py
```

Crea el tenant `public` (`public.localhost`) y un tenant de prueba `tenant1` (`tenant1.localhost`). `poc_tenant.py` es solo para desarrollo; en staging/producción se usa `bootstrap_platform` (ver [Despliegue](#despliegue)).

### 4. Confirmar que todo funciona

```bash
curl -H "Host: tenant1.localhost" http://localhost:8000/api/v1/health/
```

Debería responder `{"status": "healthy", "checks": {"database": true, "redis": true}}`. Luego abre el ERP en `http://tenant1.localhost:5173` y el panel interno en `http://public.localhost:5173/admin/login`: el frontend llama a la API por el mismo origen y el proxy de Vite la reenvía al backend.

> Todas las requests deben resolverse contra un dominio de tenant real (`tenant1.localhost`, `public.localhost`), nunca contra `localhost` a secas. `*.localhost` resuelve a `127.0.0.1` en la mayoría de navegadores y sistemas operativos sin tocar el archivo `hosts`.

## Variables de entorno

Lista completa y comentada en [`.env.example`](.env.example). Las principales:

| Variable | Descripción | Default local |
|---|---|---|
| `DEBUG` | Modo debug de Django | `True` |
| `SECRET_KEY` | Obligatoria con `DEBUG=False` | — |
| `ALLOWED_HOSTS` | Hosts permitidos, separados por coma | `*` |
| `CSRF_TRUSTED_ORIGINS` | Orígenes HTTPS de confianza (producción) | — |
| `DATABASE_URL` | Conexión a PostgreSQL | `postgres://fivuza:password@localhost:5433/fivuza_db` |
| `REDIS_URL` / `CELERY_BROKER_URL` | Redis (cache, channel layer, broker) | `redis://redis:6379/0` |
| `AWS_*`, `MEDIA_PUBLIC_BASE_URL` | Bucket S3 o S3-compatible (ver `core/storage.py`) | — |
| `SENTRY_DSN` / `SENTRY_ENVIRONMENT` | Sentry (vacío = desactivado) | — / `development` |
| `API_V1_PAGINATION_ENABLED` / `API_STANDARD_ERRORS_ENABLED` | Solo para **apagar** el contrato vigente | `True` |
| `THROTTLE_ENABLED` | Límites de peticiones (apagados en la suite) | `True` |

## Comandos útiles

```bash
# Migraciones (esquema public + todos los tenants)
docker compose exec web python manage.py migrate_schemas --shared
docker compose exec web python manage.py migrate_schemas

# Generar migraciones nuevas
docker compose exec web python manage.py makemigrations

# Sembrar/actualizar los planes comerciales
docker compose exec web python manage.py seed_plans

# Shell y logs
docker compose exec web python manage.py shell
docker compose logs web -f
```

## Pruebas y linting

```bash
# Suite completa (lenta: cada TenantTestCase crea su esquema)
docker compose exec web python manage.py test

# Una app o una clase
docker compose exec web python manage.py test ventas
docker compose exec web python manage.py test ventas.tests.test_views.SaleSyncTests

# Cobertura
docker compose exec web coverage run manage.py test && docker compose exec web coverage report

# Lint y formato
docker compose exec web ruff check .
docker compose exec web ruff format .
```

El CI (`.github/workflows/ci.yml`) corre Ruff, la suite con cobertura, `pip-audit` (informativo), `manage.py check`, migraciones pendientes, validación estricta del OpenAPI, compatibilidad del OpenAPI contra `main` y el build de Docker.

## Documentación de la API

Con el servidor corriendo, desde el dominio `public` y autenticado como `platform_staff`:

| Recurso | URL |
|---|---|
| Swagger UI | `http://public.localhost:8000/api/docs/` |
| ReDoc | `http://public.localhost:8000/api/redoc/` |
| Esquema OpenAPI | `http://public.localhost:8000/api/schema/` |
| Health check | `/api/v1/health/` (cualquier dominio) y `/healthz` (sin resolver tenant, para Railway) |

## Despliegue

Staging y producción corren en **Railway**: servicios `backend-web`, `celery-worker` y `celery-beat` desde este repo, más el `frontend` (nginx) que expone `*.fivuza.com` y reenvía `/api` y `/ws` al backend. Servicios, variables, dominios, primer despliegue y rollback: **[railway/README.md](railway/README.md)**.

`python manage.py bootstrap_platform` deja listo un entorno nuevo de forma idempotente (tenant `public`, dominios de `PUBLIC_DOMAINS`, planes y primer `SUPER_ADMIN`); corre en el pre-deploy.

## Convenciones de contribución

- **Ramas:** `feature/`, `fix/`, `chore/`, `hotfix/`, `docs/` + módulo afectado (ej. `feature/core-tenant-lifecycle-service`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/), tipo en inglés, descripción en español imperativo (`feat(core): agregar endpoint de suspensión de tenant`).
- **Pull Requests:** `main` protegida, mínimo 1 revisor distinto al autor, CI en verde obligatorio antes de mergear.
- **Estilo de código:** inglés en identificadores, español en comentarios/docstrings, solo cuando el *por qué* no sea obvio por el nombre.
- **Política de `on_delete`:** `PROTECT` por defecto; `CASCADE` solo para líneas de detalle y tablas de unión; `SET_NULL` solo para relaciones explícitamente opcionales.

Detalle en `FIVUZA_Convenciones_Codigo_v1.md` (repo [Fivuza-Docs](https://github.com/vexa-dev/Fivuza-Docs)).

## Estado del proyecto

Todas las funcionalidades de los Sprints 0–35 están construidas (inventario, ventas/POS con modo offline, caja, crédito, RR. HH., dashboard en tiempo real, gimnasio, panel interno con administración avanzada de tenants, cumplimiento de la Ley 29733). El trabajo en curso es la salida a producción en Railway y el arranque del piloto; ver el plan por bloques en `FIVUZA_Plan_Implementacion_v2.md` §2.1.

Riesgo conocido y aceptado: `djangorestframework-simplejwt` se mantiene en 5.3.x, que tiene un CVE; la 5.5.x rompe el login propio porque `usuarios.User` y `core.PlatformStaff` no son `AUTH_USER_MODEL`. `pip-audit` lo reporta en CI sin bloquear.
