# Docker Hub + Koyeb — `rahadarmannabid/distractors`

> New to this? Follow **[HOSTING.md](./HOSTING.md)** step by step; this page is the reference.

Run every command from the project root (the folder with `Dockerfile`). `docker`, `python` and
`koyeb` commands work in any terminal. **`./release.sh` is a bash script**: run it in the macOS
Terminal or a Linux shell, or on Windows in **Git Bash** (installed with Git: right-click the
project folder → *Open Git Bash here*).

> **`python` or `python3`?** Windows: `python` (or `py`). macOS / Linux: `python3` if `python` is
> not found.

The image **does not** contain `.env`, the Firebase key file or any API key. Secrets are given at
**runtime** (`--env-file .env`, Compose `env_file`, Koyeb environment variables / secrets).

---

## One-time setup

1. **Install Docker**: Docker Desktop on macOS / Windows (includes Buildx), Docker Engine +
   `docker-buildx-plugin` on Linux. Start it.
2. **Create the repository** on [hub.docker.com](https://hub.docker.com) → **Repositories** →
   **Create repository** → name **`distractors`** (public or private).
3. **Log in** with user `rahadarmannabid` and a password or an access token
   (Docker Hub → Account settings → Personal access tokens):

   ```
   docker login
   ```

4. *(Optional, for one-command Koyeb deploys)* install the
   [Koyeb CLI](https://www.koyeb.com/docs/build-and-deploy/cli/installation) and run `koyeb login`.

---

## Release: build, tag and push

```
./release.sh
```

That one command:

1. reads the last version from `VERSION` and bumps it (`1.0.3` → `v1.0.4`; the first release is `v1.0.0`),
2. refuses if that tag is already on Docker Hub (so a release never silently overwrites another),
3. builds for **linux/amd64 + linux/arm64** (Koyeb runs amd64, Apple Silicon builds arm64),
4. pushes **`rahadarmannabid/distractors:v1.0.4`** and moves **`:latest`** to it,
5. writes the new version to `VERSION`. Commit that file.

| Command | What it does |
|---------|--------------|
| `./release.sh` | patch bump: `v1.0.3` → `v1.0.4` |
| `./release.sh minor` | `v1.0.3` → `v1.1.0` |
| `./release.sh major` | `v1.0.3` → `v2.0.0` |
| `./release.sh --tag v2.0.0-rc1` | exact tag (VERSION only changes for plain `x.y.z` tags) |
| `./release.sh --koyeb APP/SERVICE` | release, then point that Koyeb service at the new tag (repeat the flag for several services) |
| `./release.sh --platforms linux/amd64` | faster: amd64 only (enough for Koyeb) |
| `./release.sh --local` | build for this machine and load it into Docker; no push, no version bump |
| `./release.sh --dry-run` | print the commands only |
| `./release.sh --no-cache` / `--force` / `--no-latest` | full rebuild / overwrite an existing tag / leave `:latest` alone |

The first multi-arch build is slow (the other architecture is emulated); later builds reuse the cache.

**Check which version is live:** `GET /health` returns `"version": "v1.0.4"`.

---

## Run locally or on your own server (Docker Compose)

Compose starts the app, Redis (shared sessions, so 4 workers) and the isolated code runner.

1. Create `.env` next to `docker-compose.yml` (see README → *Environment variables*), and add a
   runner token. Generate one with:

   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   and paste it into `.env` as `RUNNER_TOKEN=<that value>`.

2. Start it:

   ```
   docker compose up -d
   ```

3. Open <http://localhost:8000>. `http://localhost:8000/health` should show
   `"session_store": "redis"` and `"code_runner": "service"`.

| Task | Command |
|------|---------|
| Use the pushed image instead of building | `docker compose pull` then `docker compose up -d` |
| Rebuild from your source | `docker compose up -d --build` |
| Logs | `docker compose logs -f guessing_ide` |
| Stop | `docker compose down` |

Single container without Compose (no Redis → 1 worker, code runs in the app container):

```
docker run -d --name distractors -p 8000:8000 --env-file .env rahadarmannabid/distractors:latest
```

---

## Deploy on Koyeb

Use **two services in one Koyeb app**: the public web app and a private code runner. Both use the
same image.

### 1. Web service (public)

Koyeb dashboard → **Create Service** → **Docker image**:

| Setting | Value |
|---------|-------|
| Image | `docker.io/rahadarmannabid/distractors:v1.0.0` (a version tag, so each release is a new deploy) |
| Registry secret | only if the Docker Hub repo is private (your Docker Hub user + access token) |
| Service name | e.g. `web` |
| Port | `8000`, protocol **HTTP**, route `/` |
| Health check | HTTP, port `8000`, path `/health` |
| Instance | 1 GB RAM or more |

Environment variables (use Koyeb **Secrets** for anything sensitive):

| Variable | Value |
|----------|-------|
| `OPENAI_API_KEY` | secret |
| `SESSION_SECRET_KEY` | secret, a long random string (generate it with the `python -c` command above) |
| `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_DATABASE_URL`, … | from the Firebase service-account JSON (see `initialize_firebase()` in `main.py`); the JSON file itself is never in the image |
| `RUNNER_URL` | `http://runner:8100` (the runner service below) |
| `RUNNER_TOKEN` | secret, the same value as on the runner |
| `REDIS_URL` | a managed Redis URL (e.g. Upstash, Redis Cloud). With it the app runs 4 workers; without it, 1 |
| `WEB_CONCURRENCY` | *(optional)* worker count override |

Koyeb sets `PORT` itself; the image listens on it (`PORT`, else `API_PORT`, else 8000).

### 2. Runner service (private)

Same app → **Create Service** → same image:

| Setting | Value |
|---------|-------|
| Service name | `runner` (must match `RUNNER_URL`) |
| Command override | `uvicorn runner:app --host 0.0.0.0 --port 8100 --log-level warning` |
| Port | `8100`, **no public route** (private networking only) |
| Health check | HTTP, port `8100`, path `/health` |
| Environment | **only** `RUNNER_TOKEN` (and optionally `CODE_MAX_CONCURRENT`, `CODE_MEMORY_MB`). Never attach the app's secrets |

Sizing: keep `CODE_MAX_CONCURRENT × CODE_MEMORY_MB` within the instance's memory
(defaults 40 × 256 MB ≈ 10 GB at full load; lower `CODE_MAX_CONCURRENT` on smaller instances).

### 3. Every release after that

```
./release.sh --koyeb <app>/web --koyeb <app>/runner
```

This pushes the new tag and updates both services to it; Koyeb then rolls out the new version with
no downtime once `/health` passes. Without the Koyeb CLI, change the image tag in each service's
settings in the dashboard instead.

**Rollback:** `koyeb service update <app>/web --docker rahadarmannabid/distractors:v1.0.3` (or pick
the older tag in the dashboard).

---

## Student code runner: why it is separate

Student programs never run inside the main app in production. The runner (`runner.py` +
`sandbox.py`, same image):

- has **no application secrets** (no `.env`, so no OpenAI key or Firebase credentials),
- has no internet access under Compose (internal network) and no public route on Koyeb,
- runs every program with limits on CPU time, wall time, idle time, memory, output size, file
  size, open files and process count, as the unprivileged `sandbox` user when it starts as root
  (Compose does this; on Koyeb programs run as the image user, still without any secrets),
- caps how many programs run at once (`CODE_MAX_CONCURRENT`, default 40) with a short queue, allows
  one run per student at a time, and rate-limits each student (`CODE_RUNS_PER_MINUTE`, default 20).

The Compose defaults (40 × 256 MB, 6 GB, 2 CPUs) handled a simulated class of 100 students running
code at the same time (including infinite loops and output floods) with no rejected runs.

Without `RUNNER_URL`, the app runs code itself under the same limits. That is for local development:
the Python sandbox blocks file access outside the run folder, subprocesses, network and `ctypes`, but
compiled C programs are only contained by the runner container.

---

## Troubleshooting

| Issue | What to do |
|-------|------------|
| `denied: requested access to the resource is denied` | `docker login` as `rahadarmannabid`, and check the repo `distractors` exists on Docker Hub |
| `Docker is not running` | Start Docker Desktop (or `sudo systemctl start docker` on Linux) |
| `... already exists on Docker Hub` | Someone already released that version: run the release again (it bumps), or pass `--tag` |
| Koyeb: *does not support linux/amd64* | The image was built for arm64 only; release again without `--platforms linux/arm64` |
| Koyeb: health check fails | Port must be `8000` HTTP with path `/health`; read the service logs for a missing env var |
| Students lose their session / get logged out | More than one worker without Redis: set `REDIS_URL`, or `WEB_CONCURRENCY=1` |
| `/usr/bin/env: 'bash\r'` | Old image built from a Windows checkout; rebuild (the Dockerfile and `.gitattributes` now prevent it) |
| App needs `OPENAI_API_KEY` | Pass it via `--env-file`, `-e OPENAI_API_KEY=...` or a Koyeb secret, never in the image |
