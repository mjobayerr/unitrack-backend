# UniTrack — Backend

API + workers for **UniTrack**: a digital ticketing + live bus-tracking platform for a university's own bus fleet.

This repo is the **hub** — every client talks only to this API; nothing else touches Postgres/Redis directly. It is also the **source of truth for the spec and the API contract**.

## Core constraint

No IoT hardware on buses. The **helper's smartphone is the only sensor** (GPS, QR scan, seat occupancy). Revenue arrives **only** via bKash. Every flow must survive that phone going offline mid-trip.

## Stack

| Piece | Tech |
|---|---|
| API | FastAPI (async) + Uvicorn, SQLAlchemy 2.0 async + asyncpg, Pydantic v2 |
| Realtime / cache / queue | Redis — latest-state cache, pub/sub fan-out, `gps_ingest` Stream |
| Database | PostgreSQL 16 (`gps_points` partitioned monthly) |
| Workers | Python asyncio + APScheduler — GPS consumer, ETA engine, bKash reconciler, fraud sweep + report aggregation (same repo, separate process) |
| Edge / deploy | Nginx (TLS, WS upgrade) · Docker Compose on a single 2–4 GB VPS |
| External | bKash Checkout (PGW) · Mapbox Directions (`driving-traffic`) · SMTP |

- **Redis = "now"** (positions, ETAs, seats). **Postgres = "forever"** (identity, money, history).
- All third-party calls are server-side — quotas/secrets never reach a client.

## Spec

- `docs/spec.md` — full spec as grep-able markdown (grep by section, e.g. `## 6.` data model, `### 7.5` offline tickets, `## 8` auth matrix).
- `unitrack software specs n arch.html` — same spec rendered with diagrams (open in a browser).

## Sibling repos

- **[unitrack-web](https://github.com/mjobayerr/unitrack-web)** — Next.js student PWA + admin dashboard.
- **[unitrack-helper](https://github.com/mjobayerr/unitrack-helper)** — Flutter helper app (the on-bus sensor).

Clients stay in sync via this API's generated **OpenAPI** contract (`/openapi.json`).

## Status

Pre-code (greenfield). Build order: **P1 money & identity** → P2 live ops → P3 validation & ETA → P4 reports & polish.

First slice: scaffold (compose: postgres/redis/api/worker/nginx) + identity core — `users`/`students`/`helpers`, JWT access(15m)/refresh(30d), server-side varsity-email domain gate, helper `pending_approval`.
