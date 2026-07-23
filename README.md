# Fivuza — Backend

[![Backend CI](https://github.com/vexa-dev/FivuzaBackend/actions/workflows/ci.yml/badge.svg)](https://github.com/vexa-dev/FivuzaBackend/actions/workflows/ci.yml)

API REST del **ERP SaaS multi-tenant** de Fivuza, orientado a pequeños y medianos negocios (bodegas, gimnasios, tiendas de retail). Construido con Django + Django REST Framework, con aislamiento de datos por esquema de PostgreSQL (schema-per-tenant vía [django-tenants](https://django-tenants.readthedocs.io/)).

> Proyecto privado. Repositorio complementario: [FivuzaFrontend](https://github.com/vexa-dev/FivuzaFrontend) (React + Vite).

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
- [Convenciones de contribución](#convenciones-de-contribución)
- [Estado del proyecto](#estado-del-proyecto)

---

## Stack técnico

| Categoría | Tecnología |
|---|---|
| Lenguaje / Framework | Python 3.12, Django 5.0, Django REST Framework |
| Multi-tenancy | django-tenants (aislamiento por esquema de PostgreSQL) |
| Base de datos | PostgreSQL 15 |
| Autenticación | JWT (`djangorestframework-simplejwt`), dos flujos separados (`platform_staff` / `tenant.users`) |
| Async / tiempo real | Django Channels + Daphne (ASGI), Celery + Redis |
| Documentación de API | drf-spectacular (OpenAPI / Swagger / ReDoc) |
| Observabilidad | Sentry |
| CORS | django-cors-headers |
| Linting / formato | [Ruff](https://docs.astral.sh/ruff/) |
| CI | GitHub Actions (lint → test → build) |
| Contenedores | Docker Compose (db, redis, web, celery_worker, celery_beat, frontend) |

## Arquitectura

El sistema opera como **SaaS multi-tenant con aislamiento por esquemas**: cada negocio cliente (tenant) tiene su propio esquema físico en PostgreSQL, completamente separado del resto. Un esquema `public` centraliza la gestión de la plataforma (tenants, planes, suscripciones, equipo interno de Fivuza).

El código está organizado en **5 apps de Django**, no necesariamente alineadas 1:1 con lo que se vende comercialmente:

| App | Contiene | Esquema |
|---|---|---|
| `core` | Tenants, planes, suscripciones, equipo interno (`platform_staff`) | `public` (compartida) |
| `usuarios` | RBAC (roles/permisos), autenticación de `tenant.users`, RRHH | Por tenant |
| `inventario` | Catálogo, variantes, stock, Kardex, compras, impuestos | Por tenant |
| `ventas` | Punto de venta, pagos, devoluciones, crédito/saldo de clientes, caja | Por tenant |
| `dashboard` | Agregación de métricas de solo lectura sobre las otras 3 apps | Por tenant |

Cada app de negocio sigue una arquitectura en capas: `ViewSet → Serializer → Service → Model`, donde toda la lógica de negocio vive en `services.py` (nunca en el ViewSet ni en el modelo).

## Estructura del proyecto

```
FivuzaBackend/
├── config/          # settings, urls, asgi/wsgi, celery
├── core/            # esquema public: tenants, planes, suscripciones, platform_staff
├── usuarios/        # RBAC + RRHH (esquema tenant)
├── inventario/      # catálogo, stock, compras, impuestos (esquema tenant)
├── ventas/          # POS, caja, devoluciones, créditos (esquema tenant)
└── dashboard/       # métricas agregadas (esquema tenant)
```

Cada app de negocio comparte la misma estructura interna:

```
<app>/
├── models.py          # o models/ como paquete si crece (un archivo por dominio)
├── serializers.py
├── views.py
├── urls.py
├── permissions.py     # permisos específicos del módulo
├── services.py        # lógica de negocio (SaleService, StockService, etc.)
├── tasks.py           # tareas de Celery propias del módulo
├── signals.py         # ej. post_save que dispara eventos
└── tests/
    ├── test_models.py
    ├── test_views.py
    └── test_integration.py
```

## Primeros pasos

### Requisitos

- Docker y Docker Compose
- (Opcional) Python 3.12 local, si prefieres correr comandos fuera de Docker

### 1. Clonar y configurar variables de entorno

```bash
git clone https://github.com/vexa-dev/FivuzaBackend.git
cd FivuzaBackend
cp .env.example .env
```

Completa `.env` con al menos `SECRET_KEY` (genera uno con el comando de abajo) y, si vas a usar Sentry, tu `SENTRY_DSN`.

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### 2. Levantar el entorno

```bash
docker compose up -d
```

Esto levanta `db` (PostgreSQL), `redis`, `web` (Daphne en `:8000`), `celery_worker`, `celery_beat` y `frontend` (si el repo de `FivuzaFrontend` está clonado como carpeta hermana, `../FivuzaFrontend`).

### 3. Aplicar migraciones y crear un tenant de prueba

```bash
docker compose exec web python manage.py migrate_schemas --shared
docker compose exec web python poc_tenant.py
```

Esto crea el tenant `public` (esquema global) y un tenant de prueba `tenant1`, con dominio `tenant1.localhost`.

### 4. Confirmar que todo funciona

```bash
curl -H "Host: tenant1.localhost" http://localhost:8000/api/v1/health/
```

Debería responder `{"status": "healthy", "checks": {"database": true, "redis": true}}`.

> **Nota sobre multi-tenancy en desarrollo:** todas las requests deben resolverse contra un dominio de tenant real (`tenant1.localhost`, `public.localhost`), nunca contra `localhost` a secas — `*.localhost` resuelve automáticamente a `127.0.0.1` en la mayoría de navegadores/OS modernos, sin necesidad de tocar el archivo `hosts`.

## Variables de entorno

| Variable | Descripción | Default local |
|---|---|---|
| `DEBUG` | Modo debug de Django | `True` |
| `SECRET_KEY` | Clave secreta de Django (generar una propia, nunca usar el fallback inseguro en producción) | — |
| `ALLOWED_HOSTS` | Hosts permitidos, separados por coma (`*` en dev) | `*` |
| `DATABASE_URL` | Cadena de conexión a PostgreSQL | `postgres://fivuza:password@localhost:5433/fivuza_db` |
| `REDIS_URL` | Cadena de conexión a Redis | `redis://redis:6379/0` |
| `CELERY_BROKER_URL` | Broker de Celery | `redis://redis:6379/0` |
| `CORS_ALLOWED_ORIGINS` | Orígenes permitidos para CORS, separados por coma | `http://localhost:5173` |
| `SENTRY_DSN` | DSN del proyecto en Sentry (vacío = Sentry desactivado) | — |
| `SENTRY_ENVIRONMENT` | Nombre de entorno reportado a Sentry | `development` |

## Comandos útiles

```bash
# Migraciones (esquema public + todos los tenants)
docker compose exec web python manage.py migrate_schemas --shared

# Migraciones de un tenant especifico
docker compose exec web python manage.py migrate_schemas --schema=tenant1

# Generar migraciones nuevas
docker compose exec web python manage.py makemigrations

# Shell interactivo
docker compose exec web python manage.py shell

# Levantar solo un servicio
docker compose up -d web

# Ver logs
docker compose logs web -f
```

## Pruebas y linting

```bash
# Suite completa de tests
docker compose exec web python manage.py test

# Tests de una sola app
docker compose exec web python manage.py test core

# Lint
docker compose run --rm web ruff check .

# Formato (aplica cambios)
docker compose run --rm web ruff format .
```

El pipeline de CI (`.github/workflows/ci.yml`) corre `lint → test → build` en cada Pull Request y en cada push a `main`.

## Documentación de la API

Con el servidor corriendo:

| Recurso | URL |
|---|---|
| Swagger UI | `http://localhost:8000/api/docs/` |
| ReDoc | `http://localhost:8000/api/redoc/` |
| Esquema OpenAPI (JSON) | `http://localhost:8000/api/schema/` |
| Health check | `http://localhost:8000/api/v1/health/` |

## Convenciones de contribución

- **Ramas:** `feature/`, `fix/`, `chore/`, `hotfix/`, `docs/` + módulo afectado (ej. `feature/core-tenant-lifecycle-service`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/), tipo en inglés, descripción en español imperativo (`feat(core): agregar endpoint de suspensión de tenant`).
- **Pull Requests:** `main` protegida, mínimo 1 revisor distinto al autor, CI en verde obligatorio antes de mergear.
- **Estilo de código:** inglés en identificadores (variables, funciones, modelos), español en comentarios/docstrings — solo cuando el *por qué* no sea obvio por el nombre.
- **Política de `on_delete`:** `PROTECT` por defecto en toda ForeignKey; `CASCADE` solo para líneas de detalle y tablas de unión; `SET_NULL` solo para relaciones explícitamente opcionales.

Estas y otras reglas están detalladas en la Guía de Convenciones de Código del proyecto (documento compartido del equipo, fuera de este repositorio).

## Estado del proyecto

Desarrollo guiado por sprints semanales (ver Plan de Implementación del equipo). Estado actual:

- ✅ **Sprint 0** — entorno, CI/CD, POC de django-tenants, Sentry, health check real.
- ✅ **Sprint 1** — app `core` completa: modelo de datos v5 (48 tablas + 2 vistas materializadas), autenticación JWT de `platform_staff`, `TenantValidatedJWTAuthentication`, `TenantLifecycleService` (suspensión/reactivación), CRUD completo de tenants/planes/suscripciones.
- 🔄 **Sprint 2** — app `usuarios`: roles, permisos, `PermissionService`, login funcional de `tenant.users`.

Meta: MVP del módulo de Inventario funcional de punta a punta para el piloto.
