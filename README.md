# UniTrack — Backend

API + workers for **UniTrack**: a digital ticketing + live bus-tracking platform for a university's own bus fleet.

This repo is the **hub** — every client talks only to this API; nothing else touches Postgres/Redis/Elasticsearch directly. It is also the **source of truth for the spec and the API contract**.

## Core constraint

No IoT hardware on buses. The **helper's smartphone is the only sensor** (GPS, QR scan, seat occupancy). Revenue arrives **only** via bKash. Every flow must survive that phone going offline mid-trip.

## Stack

| Piece | Tech |
|---|---|
| API | FastAPI (async) + Uvicorn, SQLAlchemy 2.0 async + asyncpg, Pydantic v2 |
| Realtime / cache / queue | Redis — latest-state cache, pub/sub fan-out, `gps_ingest` Stream |
| Relational DB | PostgreSQL 16 — identity, fleet, commerce, history |
| GPS store | **Elasticsearch 8** — all GPS fixes as `geo_point` docs (geo queries) |
| Workers | Python asyncio — GPS→ES indexer (more jobs later), same repo / separate process |
| Edge / deploy | Nginx (TLS, WS upgrade) · Docker Compose on a single 2–4 GB VPS |
| External (later) | bKash Checkout · Mapbox Directions · SMTP |

- **Redis = "now"** (latest position, ETAs, seats). **Postgres = "forever"** (identity, money). **Elasticsearch = GPS** (geo search over the fix history).
- All third-party calls are server-side — quotas/secrets never reach a client.

> **Note on Elasticsearch.** The spec (`docs/spec.md` §5.1) originally dropped ES from v1. It was later re-introduced **as the sole GPS store** on request, to get geo queries (nearby / viewport / heatmap) that Postgres can't do without PostGIS. Postgres no longer holds GPS at all — migration `b7f3c1a9d2e4` drops the old `gps_points` table. This is an intentional deviation from the written spec.

---

## What's done

### 1. Identity core (P1)
- `users` / `students` / `helpers` tables; roles `student` / `helper` / `admin`.
- **JWT** access (15 min) + refresh (30 day) — [`app/core/security.py`](app/core/security.py).
- **Server-side varsity-email gate**: student signup rejects non-allow-listed domains (403), not just in the UI — [`app/api/routes/auth.py`](app/api/routes/auth.py).
- Helpers register as `pending_approval`; helper-only endpoints are gated on `status='approved'` — [`app/api/deps.py`](app/api/deps.py) `get_current_helper`.
- Email verification link is logged to stdout (SMTP is a later phase).

### 2. Live GPS pipeline → Elasticsearch (P2, partial)
The end-to-end path **helper phone → API → Redis → worker → Elasticsearch** is wired:

1. Helper `POST /helper/gps` with a batch of fixes ([`app/api/routes/helper.py`](app/api/routes/helper.py)). The endpoint:
   - checks the bus exists,
   - writes the newest fix to Redis `bus:{id}:pos` (HASH, TTL 60 s) — the "latest position",
   - publishes to `fleet:ch` (admin live-map fan-out),
   - `XADD`s every fix to the `gps_ingest` Redis Stream.
2. The worker ([`app/worker/gps_es_indexer.py`](app/worker/gps_es_indexer.py)) reads the stream via consumer group `es_indexers` and **bulk-indexes** each fix into the ES `gps_points` index as a `geo_point` doc (the stream id is the ES doc id → reprocess-safe).
3. Read side: `GET /track/nearby?lat=&lng=&radius_km=` runs an ES `geo_distance` query, collapses to one hit per bus, closest first ([`app/api/routes/tracking.py`](app/api/routes/tracking.py)).

```
                                   ┌──────────► Redis bus:{id}:pos  (latest, TTL 60s)
helper POST /helper/gps ──► API ──┼──────────► Redis fleet:ch      (live-map pub/sub)
                                   └─ XADD ───► gps_ingest stream
                                                     │
                                     group es_indexers │  (worker)
                                                     ▼
                                            Elasticsearch gps_points  ◄── GET /track/nearby
```

### 3. Ops / scaffold
- Docker Compose: `postgres`, `redis`, `elasticsearch`, `api`, `worker`, `nginx` — [`docker-compose.yml`](docker-compose.yml).
- Alembic migrations (identity core → fleet → drop gps_points) — [`alembic/versions/`](alembic/versions/).
- Dev seed scripts: initial admin + a bus/approved-helper for GPS testing — [`scripts/`](scripts/).

---

## Repo layout

```
app/
  main.py                 FastAPI app factory + /health
  core/
    config.py             pydantic-settings (env)
    security.py           JWT encode/decode, argon2 hashing
    redis.py              async client, stream + keyspace helpers
    elasticsearch.py      async client, gps_points geo_point mapping, ensure-index
  db/                     async engine + session, declarative Base
  models/                 user, fleet (Bus)  — no GPS model (ES-only)
  schemas/                auth, gps request/response (Pydantic)
  api/
    deps.py               auth guards (get_current_user/helper, require_role)
    routes/auth.py        register / verify / login / refresh / me
    routes/helper.py      POST /helper/gps  (ingest)
    routes/tracking.py    GET /track/nearby (ES geo query)
  worker/
    __main__.py           worker entrypoint (asyncio.gather of jobs)
    gps_es_indexer.py     gps_ingest stream → Elasticsearch
alembic/                  migrations
scripts/                  seed_admin, dev_seed_fleet
deploy/nginx.conf         edge config
docs/spec.md              full grep-able spec
```

---

## Run (dev)

```bash
cp .env.example .env            # then set a real JWT_SECRET

# Elasticsearch needs a high mmap limit or it won't boot:
sudo sysctl -w vm.max_map_count=262144      # persist in /etc/sysctl.conf for reboots

# All services (postgres, redis, elasticsearch, api, worker, nginx):
docker compose up -d --build
docker compose run --rm api alembic upgrade head
```

Local Python (uv) instead of the api/worker containers:

```bash
uv sync
docker compose up -d postgres redis elasticsearch
uv run alembic upgrade head
uv run uvicorn app.main:app --reload        # http://localhost:8000/docs
uv run python -m app.worker                 # GPS → ES indexer
```

### Smoke-test the GPS pipeline (no phone needed)

```bash
# 1. seed a bus (prints bus_id) + approve a helper
uv run python -m scripts.seed_admin                       # admin login
BUS_REG_NO=DHK-01 uv run python -m scripts.dev_seed_fleet # -> bus_id=<uuid>
# register + approve + login a helper to get an access token (see Auth table)

# 2. post a fix
curl -X POST localhost:8000/helper/gps -H "authorization: Bearer <token>" \
  -H 'content-type: application/json' \
  -d '{"bus_id":"<uuid>","points":[{"lat":23.78,"lng":90.40,"ts":"2026-07-14T10:00:00Z"}]}'

# 3. read it back out of Elasticsearch
curl "localhost:8000/track/nearby?lat=23.78&lng=90.40&radius_km=5"
```

---

## API (current)

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register/student` | Rejects non-varsity email domains **server-side** (403). |
| POST | `/auth/register/helper` | Creates a `pending_approval` account. |
| GET | `/auth/verify-email?token=` | Verify link logged to stdout (SMTP later). |
| POST | `/auth/login` | Access + refresh tokens; blocks non-active accounts. |
| POST | `/auth/refresh` | New token pair from a refresh token. |
| GET | `/auth/me` | Current user (Bearer access token). |
| GET | `/admin/helpers` | Approval queue; `?helper_status=pending` to filter. **admin** |
| POST | `/admin/helpers/{id}/approve` | Approve a helper so they can send GPS. **admin** |
| POST | `/admin/users/{id}/suspend` | Suspend an account; effective immediately. **admin** |
| POST | `/helper/gps` | Ingest a batch of fixes (approved helper only). |
| GET | `/track/nearby` | Buses within `radius_km`, closest first (ES `geo_distance`). |

Full contract: `GET /openapi.json` (clients generate types from it).

### Auth

Every route is authenticated and authorized except the handful in
`PUBLIC_PATHS` (register / verify / login / refresh / health / docs) — enforced
by [`tests/test_auth_coverage.py`](tests/test_auth_coverage.py), which fails the
build on any unguarded route. Guards live on the router; the caller's role and
status are resolved into a Redis-cached `Principal` (~0.15 ms, no Postgres hit
on the hot path) and invalidated on every write to `users` / `helpers`, so
suspension takes effect on the next request.

**Read [`docs/auth.md`](docs/auth.md) before adding an endpoint.** Worked
example: [`app/api/routes/admin.py`](app/api/routes/admin.py).

## Config

Env vars in [`.env.example`](.env.example): Postgres, Redis, `ELASTICSEARCH_URL`, `GPS_INDEX`, `JWT_SECRET`, token TTLs, `ALLOWED_STUDENT_EMAIL_DOMAINS`. Real `.env` is gitignored — never commit secrets.

## What's next

- **Trip lifecycle**: bind every GPS fix / redemption / seat report to a `trips` row (spec §6). Right now ingest is trip-agnostic.
- **Live tracking WebSocket**: `/ws/track/{route_id}` fan-out of position + ETA + seats (spec §7.3 step 4).
- **ETA engine**: Mapbox `driving-traffic` worker job (spec §7.4).
- **Tickets & offline QR** (spec §7.2/§7.5), **bKash** (§9), **fraud sweep**, **reports** (§10).
- **ES hardening**: single-node ES is not durable — add a replica + snapshot policy before production; report/fraud jobs must query ES, not Postgres joins.

Build order: **P1 money & identity → P2 live ops → P3 validation & ETA → P4 reports & polish**.

## Sibling repos

- **[unitrack-web](https://github.com/mjobayerr/unitrack-web)** — Next.js student PWA + admin dashboard.
- **[unitrack-helper](https://github.com/mjobayerr/unitrack-helper)** — Flutter helper app (the on-bus sensor).

---

_Parts of this project were built with the help of AI._
