"""End-to-end smoke test against a running UniTrack API.

Complements `tests/`, which is deliberately dependency-free and therefore
cannot catch anything that only breaks against a real database, Redis or
Elasticsearch. This walks the whole helper journey — register, approve, sign
in, start a trip, stream GPS, report seats, raise and resolve an alert, end the
trip, read the fix back out of Elasticsearch, then suspend the account and
confirm access dies on the very next request.

It asserts the failure paths too, because those are the ones that rot quietly:
a double trip start must be refused by the partial unique index, seats without
a trip must be a 409, and a helper must not reach an admin route.

Usage — with the stack up and migrations applied:

    python -m scripts.seed_admin          # ADMIN_EMAIL / ADMIN_PASSWORD
    python -m scripts.dev_seed_fleet      # a bus
    python -m scripts.dev_seed_routes     # stops + routes
    python -m scripts.smoke_test

Every run creates a fresh helper account, so it is safe to repeat. Point it
elsewhere with SMOKE_BASE_URL. Never run it against production: it suspends the
account it creates and writes real rows.
"""

import os
import sys
import time
import uuid

import httpx

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@ulab.edu.bd")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# How long to wait for the worker to move a fix from the Redis stream into ES.
INDEX_TIMEOUT_S = 15

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  <- ' + detail if detail else ''}")


def main() -> int:
    if not ADMIN_PASSWORD:
        print("Set ADMIN_PASSWORD (the one used by scripts.seed_admin).")
        return 2

    c = httpx.Client(base_url=BASE_URL, timeout=20.0)

    r = c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    if r.status_code != 200:
        return 1
    admin_h = {"authorization": f"Bearer {r.json()['access_token']}"}

    # A fresh account per run keeps this repeatable.
    helper_email = f"helper-{uuid.uuid4().hex[:8]}@buscrew.com.bd"
    helper_pw = "HelperPass!2026"
    r = c.post(
        "/auth/register/helper",
        json={
            "email": helper_email,
            "password": helper_pw,
            "name": "Smoke Helper",
            "phone": "01700000000",
        },
    )
    check("register helper", r.status_code == 201, f"{r.status_code} {r.text[:160]}")

    r = c.post("/auth/login", json={"email": helper_email, "password": helper_pw})
    check("unapproved helper cannot sign in", r.status_code == 403, f"got {r.status_code}")

    r = c.get("/admin/helpers", params={"helper_status": "pending"}, headers=admin_h)
    rows = [h for h in r.json() if h["email"] == helper_email] if r.status_code == 200 else []
    check("new helper is in the approval queue", len(rows) == 1, f"{r.status_code}")
    if not rows:
        return 1
    helper_id, user_id = rows[0]["helper_id"], rows[0]["user_id"]

    r = c.post(f"/admin/helpers/{helper_id}/approve", headers=admin_h)
    check("admin approves helper", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    r = c.post(f"/admin/helpers/{helper_id}/approve", headers=admin_h)
    check("re-approving is 409", r.status_code == 409, f"got {r.status_code}")

    # Approval invalidated the cached Principal, so this must work immediately.
    r = c.post("/auth/login", json={"email": helper_email, "password": helper_pw})
    check("approved helper signs in", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    helper_h = {"authorization": f"Bearer {r.json()['access_token']}"}

    check(
        "helper is refused an admin route",
        c.get("/admin/helpers", headers=helper_h).status_code == 403,
    )
    check(
        "unauthenticated tracking is refused",
        c.get("/track/nearby", params={"lat": 23.78, "lng": 90.40}).status_code in (401, 403),
    )

    r = c.get("/fleet/buses", headers=helper_h)
    check("fleet buses", r.status_code == 200 and r.json(), f"{r.status_code} — seed a bus first?")
    if r.status_code != 200 or not r.json():
        return 1
    bus = r.json()[0]

    r = c.get("/fleet/routes", headers=helper_h)
    check(
        "fleet routes",
        r.status_code == 200 and r.json(),
        f"{r.status_code} — seed routes first?",
    )
    if r.status_code != 200 or not r.json():
        return 1
    route = r.json()[0]

    r = c.get(f"/fleet/routes/{route['id']}", headers=helper_h)
    check(
        "route detail carries ordered stops",
        r.status_code == 200 and len(r.json().get("stops", [])) > 0,
        f"{r.status_code}",
    )

    check(
        "seats without a trip is 409",
        c.post("/helper/seats", json={"occupied": 10}, headers=helper_h).status_code == 409,
    )

    # An alert must never be refused for want of a trip — see routes/helper.py.
    r = c.post("/helper/alerts", json={"type": "breakdown"}, headers=helper_h)
    check("alert without a trip is allowed", r.status_code == 201, f"{r.status_code}")
    check("severity is assigned by the server", r.json().get("severity") == "critical")

    r = c.get("/helper/trips/active", headers=helper_h)
    check("no active trip initially", r.status_code == 200 and r.json() is None, f"{r.status_code}")

    r = c.post(
        "/helper/trips/start",
        json={"bus_id": bus["id"], "route_id": route["id"]},
        headers=helper_h,
    )
    check("start trip", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    trip = r.json()

    r = c.post(
        "/helper/trips/start",
        json={"bus_id": bus["id"], "route_id": route["id"]},
        headers=helper_h,
    )
    check("double start is refused by the partial unique index", r.status_code == 409)

    r = c.get("/helper/trips/active", headers=helper_h)
    check(
        "active trip is returned",
        r.status_code == 200 and r.json() and r.json()["trip_id"] == trip["id"],
        f"{r.status_code} {r.text[:160]}",
    )

    r = c.post(
        "/helper/gps",
        json={
            "bus_id": bus["id"],
            "points": [
                {"lat": 23.7561, "lng": 90.3720, "ts": "2026-07-23T09:00:00Z", "speed": 8.0},
                {"lat": 23.7580, "lng": 90.3750, "ts": "2026-07-23T09:00:05Z", "speed": 9.5},
            ],
        },
        headers=helper_h,
    )
    check("gps ingest", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    check("fixes are bound to the trip", r.json().get("trip_id") == trip["id"])

    r = c.post(
        "/helper/gps",
        json={
            "bus_id": str(uuid.uuid4()),
            "points": [{"lat": 23.7, "lng": 90.4, "ts": "2026-07-23T09:00:10Z"}],
        },
        headers=helper_h,
    )
    check("gps for another bus is refused", r.status_code == 409, f"got {r.status_code}")

    r = c.post("/helper/seats", json={"occupied": 42}, headers=helper_h)
    check("seat report", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    seats = r.json()
    check(
        "free seats never go negative",
        seats["free"] == max(seats["capacity"] - seats["occupied"], 0),
        str(seats),
    )
    check(
        "an absurd count is rejected",
        c.post("/helper/seats", json={"occupied": 999}, headers=helper_h).status_code == 422,
    )

    r = c.post("/helper/alerts", json={"type": "sos", "lat": 23.75, "lng": 90.37}, headers=helper_h)
    check("sos alert", r.status_code == 201, f"{r.status_code}")
    check("sos is bound to the trip", r.json().get("trip_id") == trip["id"])
    alert_id = r.json()["id"]

    r = c.get("/admin/alerts", headers=admin_h)
    check("admin console lists open alerts", r.status_code == 200 and r.json(), f"{r.status_code}")
    check("critical sorts first", r.json()[0]["severity"] == "critical")

    r = c.post(f"/admin/alerts/{alert_id}/acknowledge", headers=admin_h)
    check("acknowledge", r.status_code == 200 and r.json()["status"] == "acknowledged")
    r = c.post(
        f"/admin/alerts/{alert_id}/resolve",
        json={"note": "Closed by smoke test"},
        headers=admin_h,
    )
    check("resolve", r.status_code == 200 and r.json()["status"] == "resolved")

    r = c.post("/helper/trips/end", headers=helper_h)
    check(
        "end trip",
        r.status_code == 200 and r.json()["status"] == "completed",
        f"{r.status_code}",
    )
    check(
        "ending twice is 409",
        c.post("/helper/trips/end", headers=helper_h).status_code == 409,
    )

    print("      (waiting for the worker to index into Elasticsearch...)")
    found = False
    for _ in range(INDEX_TIMEOUT_S):
        time.sleep(1)
        r = c.get(
            "/track/nearby",
            params={"lat": 23.7561, "lng": 90.3720, "radius_km": 5},
            headers=helper_h,
        )
        if r.status_code == 200 and r.json().get("count", 0) > 0:
            found = True
            break
    check(
        "fixes reach Elasticsearch and come back from /track/nearby",
        found,
        "" if found else "is the worker running?",
    )

    # The whole point of invalidate_principal(): revocation on the next request,
    # not whenever the cache happens to expire.
    r = c.post(f"/admin/users/{user_id}/suspend", headers=admin_h)
    check("suspend the helper", r.status_code == 204, f"got {r.status_code}")
    r = c.get("/fleet/buses", headers=helper_h)
    check("suspended account is refused immediately", r.status_code == 401, f"got {r.status_code}")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
