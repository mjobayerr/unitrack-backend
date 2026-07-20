# Developing unitrack-backend on Windows (VS Code)

Step-by-step setup for running and developing the UniTrack API on a Windows PC. Follow the steps **in order** — later steps assume earlier ones are done.

Two ways to run the stack (both covered below):

- **A. Everything in Docker** — simplest, no local Python needed. Good for "just run it".
- **B. Infra in Docker, API/worker in local Python (uv)** — hot-reload, debugger, fastest edit loop. **Recommended for development.**

---

## 1. Enable virtualization + WSL 2

Docker Desktop on Windows runs on WSL 2.

1. Check virtualization is enabled: Task Manager → Performance → CPU → "Virtualization: Enabled". If disabled, enable Intel VT-x / AMD-V in BIOS/UEFI first.
2. Open **PowerShell as Administrator** and run:

   ```powershell
   wsl --install
   ```

   This enables the WSL feature and installs a default Ubuntu distro.
3. Reboot when prompted.
4. Verify:

   ```powershell
   wsl --status
   wsl --update        # make sure the WSL kernel is current
   ```

   You want "Default Version: 2".

## 2. Install Docker Desktop

1. Download from <https://www.docker.com/products/docker-desktop/> and run the installer.
2. During install, keep **"Use WSL 2 instead of Hyper-V"** checked.
3. Reboot / sign out if the installer asks.
4. Start Docker Desktop, wait for the whale icon to say "running".
5. Settings → General → confirm **"Use the WSL 2 based engine"** is on.
6. Verify in a normal PowerShell:

   ```powershell
   docker --version
   docker compose version
   docker run --rm hello-world
   ```

### 2.1 Elasticsearch requirement: `vm.max_map_count`

Elasticsearch will **crash-loop on boot** unless the Linux VM behind Docker has a high mmap limit. On Windows this must be set inside WSL, not in Windows itself.

Make it permanent — create/edit `C:\Users\<you>\.wslconfig` with:

```ini
[wsl2]
kernelCommandLine = "sysctl.vm.max_map_count=262144"
```

Then restart WSL (this also restarts Docker Desktop's backend):

```powershell
wsl --shutdown
```

Start Docker Desktop again. One-off alternative (lost on reboot):

```powershell
wsl -d docker-desktop sysctl -w vm.max_map_count=262144
```

### 2.2 Make sure `docker` works in cmd/PowerShell (not just inside WSL)

Docker Desktop's CLI (`docker.exe`) lives on the **Windows** side and talks to the Linux VM through a named pipe — you never need to open a WSL/Linux shell to use it. Every `docker` command in this doc runs straight in PowerShell or `cmd.exe`.

The installer normally adds it to PATH automatically:

```powershell
where.exe docker
```

Expect something like `C:\Program Files\Docker\Docker\resources\bin\docker.exe`. If instead you get "not found":

1. Close and reopen your terminal (PATH changes need a fresh shell) and retry.
2. If still missing, add it yourself: Windows Search → **"Edit the system environment variables"** → **Environment Variables** → under **System variables**, select `Path` → **Edit** → **New** → paste `C:\Program Files\Docker\Docker\resources\bin` → OK on every dialog → open a **new** terminal.
3. Verify:

   ```powershell
   docker --version
   docker compose version
   ```

From here on, `docker` and `docker compose` are just normal commands — build, run, and drop containers straight from the terminal in VS Code, no Linux shell needed.

## 3. Install Git

1. Download from <https://git-scm.com/download/win>, install with defaults.
2. Recommended: keep Unix line endings in the repo (shell scripts and Docker builds care):

   ```powershell
   git config --global core.autocrlf input
   ```

## 4. Install Python 3.12+ and uv

The project requires **Python ≥ 3.12** and uses **[uv](https://docs.astral.sh/uv/)** for dependency management (`pyproject.toml` + `uv.lock`).

1. Install uv (PowerShell):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Close and reopen the terminal so `uv` is on PATH.
2. You do **not** need to install Python separately — uv downloads a managed Python 3.12 automatically on first `uv sync`. (If you prefer a system Python, install 3.12+ from <https://www.python.org/downloads/> and check "Add python.exe to PATH".)
3. Verify:

   ```powershell
   uv --version
   ```

## 5. Install VS Code + extensions

1. Download from <https://code.visualstudio.com/> and install.
2. Install these extensions (Ctrl+Shift+X):
   - **Python** (`ms-python.python`) + **Pylance** — language support, debugging.
   - **Ruff** (`charliermarsh.ruff`) — linting/formatting; the project's ruff config lives in `pyproject.toml`.
   - **Docker** (`ms-azuretools.vscode-docker`) — manage containers/logs from the editor.
   - Optional: **WSL** (`ms-vscode-remote.remote-wsl`), **Even Better TOML**.

## 6. Clone and configure the repo

```powershell
git clone https://github.com/mjobayerr/unitrack-backend.git
cd unitrack-backend
code .
```

Create your env file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set a real `JWT_SECRET`:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output as `JWT_SECRET=...`. Never commit `.env` (it is gitignored).

---

## 7. Option A — run everything in Docker

All six services: `postgres`, `redis`, `elasticsearch`, `api`, `worker`, `nginx`.

```powershell
docker compose up -d --build
docker compose run --rm api alembic upgrade head   # apply DB migrations
```

- API: <http://localhost:8000/docs> (also via nginx on port 80)
- Check health: <http://localhost:8000/health>
- Logs: `docker compose logs -f api worker`
- Stop: `docker compose down` (add `-v` to wipe data volumes)

Code changes require a rebuild (`docker compose up -d --build api worker`), which is why Option B is better for day-to-day development.

## 8. Option B — infra in Docker, API/worker local (recommended for dev)

### 8.1 Start only the infrastructure

```powershell
docker compose up -d postgres redis elasticsearch
```

### 8.2 Point `.env` at localhost

The defaults in `.env.example` use Docker-internal hostnames (`postgres`, `redis`, `elasticsearch`), which don't resolve from Windows. For local runs, change these lines in `.env`:

```ini
POSTGRES_HOST=localhost
POSTGRES_PORT=55432          # compose maps host 55432 -> container 5432
REDIS_HOST=localhost
ELASTICSEARCH_URL=http://localhost:9200
```

> Note: if you later switch back to Option A, the `api`/`worker` containers read the same `.env` via `env_file` — revert these lines first (or keep two env files and swap).

### 8.3 Install dependencies

```powershell
uv sync
```

Creates `.venv/` with all runtime + dev dependencies. In VS Code, select the interpreter (Ctrl+Shift+P → "Python: Select Interpreter" → `.venv\Scripts\python.exe`).

### 8.4 Migrate, then run

```powershell
uv run alembic upgrade head                  # DB migrations
uv run uvicorn app.main:app --reload         # API with hot-reload -> http://localhost:8000/docs
```

In a second terminal, the worker (GPS → Elasticsearch indexer):

```powershell
uv run python -m app.worker
```

### 8.5 Seed dev data

```powershell
uv run python -m scripts.seed_admin                            # initial admin account
$env:BUS_REG_NO = "DHK-01"; uv run python -m scripts.dev_seed_fleet   # prints bus_id=<uuid>
```

## 9. Smoke-test the GPS pipeline

No phone needed. Register + approve + login a helper to get an access token (endpoints in README "API" table), then:

```powershell
# Post a GPS fix (use curl.exe — plain `curl` is a PowerShell alias with different flags)
curl.exe -X POST localhost:8000/helper/gps -H "authorization: Bearer <token>" `
  -H "content-type: application/json" `
  -d "{\"bus_id\":\"<uuid>\",\"points\":[{\"lat\":23.78,\"lng\":90.40,\"ts\":\"2026-07-20T10:00:00Z\"}]}"

# Read it back (worker must be running — it indexes the fix into Elasticsearch)
curl.exe "localhost:8000/track/nearby?lat=23.78&lng=90.40&radius_km=5"
```

## 10. Docker command reference

Everything below runs in plain PowerShell/cmd (§2.2) — `docker compose` reads `docker-compose.yml` in the current folder, so `cd` into `unitrack-backend` first.

| Task | Command |
|---|---|
| Build/start all services (background) | `docker compose up -d --build` |
| Start specific services only | `docker compose up -d postgres redis elasticsearch` |
| Stop containers (keep data volumes) | `docker compose down` |
| Stop + **delete** data volumes | `docker compose down -v` |
| List running containers | `docker compose ps` |
| Tail logs (all / one service) | `docker compose logs -f` / `docker compose logs -f api` |
| Restart one service | `docker compose restart api` |
| Rebuild after code/Dockerfile change | `docker compose up -d --build api worker` |
| Run a one-off command in a new container | `docker compose run --rm api alembic upgrade head` |
| Open a shell inside a running container | `docker compose exec api bash` |
| List all images on this machine | `docker images` |
| List all containers (incl. stopped) | `docker ps -a` |
| Remove a stopped container | `docker rm <container>` |
| Remove an image | `docker rmi <image>` |
| Reclaim disk space (unused images/containers/cache) | `docker system prune` |
| Full reset (danger: wipes Postgres/Redis/ES data too) | `docker compose down -v --remove-orphans` |

`docker compose exec api bash` drops you inside the **container's** Linux shell (busybox/debian, from the `python:3.12-slim` base) — only needed to poke around a running container's filesystem; not required for normal dev.

## 11. Project file structure (Docker-relevant)

```
unitrack-backend/
  Dockerfile              How the `api`/`worker` image is built: python:3.12-slim base,
                           installs deps via uv, copies the app in, runs uvicorn on :8000.
  docker-compose.yml       Defines all 6 services (postgres, redis, elasticsearch, api,
                           worker, nginx), their ports, env, volumes, health checks.
  .dockerignore            Files excluded from the build context (.git, .venv, .env, logs, ...).
  .env                     Your local secrets/config — gitignored, read by containers via
                           `env_file: .env` in docker-compose.yml AND by local `uv run` via
                           pydantic-settings. Same file, two consumers — keep hosts in mind (§8.2).
  .env.example             Template for .env — safe to commit, no real secrets.
  deploy/
    nginx.conf             Edge reverse-proxy config used by the `nginx` service (port 80 ->
                           api:8000), incl. WebSocket upgrade headers for later /ws/* routes.
  alembic/                 DB migrations, run via `alembic upgrade head` (in-container or local).
  app/                     FastAPI app + worker source — bind-mounted only if you add a
                           `volumes:` entry; by default the image is rebuilt on every change
                           (see Option A vs B, §7-8).
  scripts/                 Dev seed scripts (seed_admin, dev_seed_fleet) — run via
                           `docker compose run --rm api python -m scripts.seed_admin`
                           or locally via `uv run`.
```

**Named volumes** (declared at the bottom of `docker-compose.yml`, managed by Docker — not plain folders): `postgres-data`, `redis-data`, `es-data`. `docker compose down` keeps them; `docker compose down -v` deletes them (fresh DB/cache/ES on next `up`). See them with `docker volume ls`.

## 12. Everyday development commands

| Task | Command |
|---|---|
| Run API (hot-reload) | `uv run uvicorn app.main:app --reload` |
| Run worker | `uv run python -m app.worker` |
| Run tests | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| New migration | `uv run alembic revision --autogenerate -m "message"` |
| Apply migrations | `uv run alembic upgrade head` |
| Add a dependency | `uv add <package>` (updates `pyproject.toml` + `uv.lock`) |
| Infra up / down | `docker compose up -d postgres redis elasticsearch` / `docker compose down` |
| Interactive API docs | <http://localhost:8000/docs> |

### VS Code debugging (optional)

`.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "API (uvicorn)",
      "type": "debugpy",
      "request": "launch",
      "module": "uvicorn",
      "args": ["app.main:app", "--reload"],
      "envFile": "${workspaceFolder}/.env"
    },
    {
      "name": "Worker",
      "type": "debugpy",
      "request": "launch",
      "module": "app.worker",
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
```

## 13. Troubleshooting (Windows-specific)

| Symptom | Fix |
|---|---|
| `elasticsearch` container exits / restarts with `max virtual memory areas vm.max_map_count [65530] is too low` | Do step 2.1 (`.wslconfig` + `wsl --shutdown`). |
| `docker` not recognized in a new terminal | Do step 2.2 (PATH). If it worked before and stopped, Docker Desktop probably isn't running — start it and wait for "running". |
| API can't reach DB when run with `uv run` | `.env` still has Docker hostnames — do step 8.2 (localhost + port 55432). |
| Port 8000/80/6379/9200 already in use | Find the owner: `netstat -ano \| findstr :8000`, stop it, or edit the port mapping in `docker-compose.yml`. |
| `/track/nearby` returns nothing after posting a fix | Worker not running — it moves fixes from the Redis stream into Elasticsearch. Start `uv run python -m app.worker`. |
| `curl` errors about unknown flags in PowerShell | Use `curl.exe`, not `curl` (PowerShell aliases `curl` to `Invoke-WebRequest`). |
| Everything is slow / laptop fan screaming | ES + Postgres + Redis idle fine, but cap WSL memory in `.wslconfig`: add `memory=4GB` under `[wsl2]`. |
