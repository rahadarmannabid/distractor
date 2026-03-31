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
