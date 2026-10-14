# Docker Hub — `rahadarmannabid/codeoracle`

The image **does not** contain your `.env` or API keys. Set secrets at **runtime** (`docker run --env-file .env`, Compose `env_file`, Kubernetes secrets, etc.).

---

## One-time setup

1. **Create the repository on Docker Hub** (if it does not exist yet):  
   [hub.docker.com](https://hub.docker.com) → **Repositories** → **Create repository**  
   - Name: **`codeoracle`**  
   - Visibility: Public or Private  

2. **Log in on your machine:**

   ```bash
   docker login
   ```

   Use your Docker Hub username (`rahadarmannabid`) and password or access token.

---

## Build and push

From the project root (where `Dockerfile` lives):

```bash
chmod +x build-and-push.sh
./build-and-push.sh v1.0.0
```

- Tags pushed: **`rahadarmannabid/codeoracle:v1.0.0`** and **`rahadarmannabid/codeoracle:latest`**
- Omit the argument to auto-tag with a timestamp: `./build-and-push.sh`

### Architectures (amd64 + arm64 by default)

The script uses **`docker buildx`** and pushes **`linux/amd64`** and **`linux/arm64`** in one manifest. That fixes *“does not support linux/amd64”* on hosts like **Koyeb** (often amd64) when you previously built only on Apple Silicon.

- **Faster single-arch build** (e.g. only amd64):

  ```bash
  DOCKER_PLATFORMS=linux/amd64 ./build-and-push.sh v1.0.0
  ```

- **Only arm64:**

  ```bash
  DOCKER_PLATFORMS=linux/arm64 ./build-and-push.sh v1.0.0
  ```

First multi-arch build may be slow (QEMU cross-build). Requires **Docker Buildx** (included in Docker Desktop).

---

## Run on a server

```bash
docker pull rahadarmannabid/codeoracle:latest
docker run -d --name codeoracle -p 8000:8000 --env-file /path/to/.env rahadarmannabid/codeoracle:latest
```

Or use **`docker-compose.yml`** locally with a `.env` next to it (same as before).

### Redis (sessions across Gunicorn workers)

Compose starts a **`redis:7-alpine`** service and sets **`REDIS_URL=redis://redis:6379/0`** by default so `active_sessions` uses Redis (safe with multiple workers). Data is persisted under the **`redis_data`** volume (AOF).

- Override **`REDIS_URL`** in `.env` if you use **managed Redis** (e.g. cloud) instead of the bundled container.
- **`GET /health`** includes **`session_store`**: `"redis"` or `"memory"` so you can confirm the app connected to Redis.
- For **`docker run`** without Compose, run Redis separately and pass **`-e REDIS_URL=redis://host:6379/0`**.

---

## Troubleshooting

| Issue | What to do |
|--------|------------|
| `denied: requested access to the resource is denied` | Run `docker login` and confirm the repo name matches `rahadarmannabid/codeoracle`. |
| Wrong CPU on the server | Rebuild with `DOCKER_PLATFORM=linux/amd64` or `linux/arm64` to match `uname -m` on the host. |
| App needs `OPENAI_API_KEY` | Pass via `--env-file` or `-e OPENAI_API_KEY=...` — not in the image. |

---

## Student code runner (required in production)

Student programs never run inside the main app in production. They run in a separate **runner**
container (`runner.py` + `sandbox.py`, same image) that:

- has **no application secrets** (no `.env`, so no OpenAI key or Firebase credentials),
- sits on an **internal network with no internet access**,
- runs every program as the unprivileged `sandbox` user with limits on CPU time, wall time, idle
  time, memory, output size, file size, open files and process count,
- caps how many programs run at once (`CODE_MAX_CONCURRENT`, default 40) with a short queue, allows
  one run per student at a time, and rate-limits each student (`CODE_RUNS_PER_MINUTE`, default 20).

### Docker Compose (one server)

`docker-compose.yml` already defines the `runner` service. Add a shared token to `.env`, then start:

```bash
echo "RUNNER_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d
docker compose exec guessing_ide curl -s localhost:8000/health   # "code_runner": "service"
```

Sizing: keep `CODE_MAX_CONCURRENT × CODE_MEMORY_MB` within the runner's memory (`mem_limit`).
The defaults (40 × 256 MB, 6 GB, 2 CPUs) handled a simulated class of 100 students running code at
the same time (including infinite loops and output floods) with no rejected runs.

### Koyeb (or any platform with separate services)

1. Deploy the same image a second time as a **private** service (no public route) named e.g. `runner`:
   - Command: `uvicorn runner:app --host 0.0.0.0 --port 8100`
   - Env: `RUNNER_TOKEN=<random secret>` only. Do **not** attach the app's secrets.
2. On the main app service set `RUNNER_URL=http://runner:8100` (the private service address) and
   the same `RUNNER_TOKEN`.

If the platform does not let the runner start as root, programs run as the image user instead of
`sandbox`; the runner still has no secrets, so isolation from your keys is kept.

Without `RUNNER_URL`, the app runs code itself under the same limits. That is meant for local
development: the Python sandbox blocks file access outside the run folder, subprocesses, network and
`ctypes`, but compiled C programs are only contained by the runner container.

