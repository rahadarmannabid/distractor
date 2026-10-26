# Hosting guide (step by step, for beginners)

This puts the site online at a Koyeb URL. It works on **Windows, macOS and Linux**: every
command is typed in a terminal from the project folder. Use the **Terminal** app on macOS / Linux;
on Windows use **Git Bash** (installed with Git in step 2: right-click the project folder →
*Open Git Bash here*), because `./release.sh` is a bash script. In Git Bash the macOS commands
below work as written.

> **`python` or `python3`?** Windows: `python`. macOS / Linux: `python3` if `python` says "not found".

## How the pieces fit

```
your computer ── ./release.sh ──►  Docker Hub  (stores the app image)
                                                    │
                                                    ▼
                                Koyeb app  ┌──────────────────────────┐
             students / instructors ──────►│ web    (public, port 8000)│──► OpenAI, Firebase,
                                           │ runner (private, 8100)   │    Redis, Gmail
                                           └──────────────────────────┘
```

- **web**: the site itself. **runner**: runs students' code, with no secrets, so student code can
  never read your API keys.
- **Firebase** keeps all study data (quizzes, sessions, surveys, logs). Koyeb's disk is wiped on
  every deploy, so nothing important is kept there.

---

## Step 1: Create the accounts (one time)

| Account | What for | Notes |
|---------|----------|-------|
| [Docker Hub](https://hub.docker.com) | stores the app image | username `rahadarmannabid`; create a repository named **`distractors`** |
| [Koyeb](https://app.koyeb.com) | runs the site | two services need a paid plan (the free instance is too small) |
| [Upstash](https://upstash.com) | free Redis (shared sessions) | create a Redis database, copy its `rediss://…` URL |
| [OpenAI](https://platform.openai.com/api-keys) | AI suggestions | create an API key |
| [Firebase](https://console.firebase.google.com) | study data | you already have project **distractor-1349f** |
| Gmail | sends instructors their sign-in code | turn on 2-step verification, then create an [App Password](https://myaccount.google.com/apppasswords) |

## Step 2: Install the tools on your computer (one time)

1. **Docker Desktop**: <https://www.docker.com/products/docker-desktop/> (Linux: Docker Engine +
   `docker-buildx-plugin`). Open it and leave it running.
2. **Python 3.10+**: <https://www.python.org/downloads/> (Windows: tick **"Add python.exe to PATH"**).
3. **Git**: <https://git-scm.com/downloads>.
4. *(Optional)* **Koyeb CLI**, for one-command deploys: <https://www.koyeb.com/docs/build-and-deploy/cli/installation>.

Check they work:

```
docker --version
python --version
git --version
```

## Step 3: Fill in your settings (`.env`)

1. Copy the template: `cp .env.example .env`
2. Open `.env` in any text editor and fill it in:

| Variable | Where to get it |
|----------|-----------------|
| `OPENAI_API_KEY` | OpenAI → API keys |
| `SESSION_SECRET_KEY` | run `python -c "import secrets; print(secrets.token_hex(32))"` and paste the output |
| `RUNNER_TOKEN` | run the same command again (a **different** value) |
| `FIREBASE_*` | Firebase console → ⚙ Project settings → **Service accounts** → **Generate new private key**. Open the downloaded JSON and copy: `project_id` → `FIREBASE_PROJECT_ID`, `private_key_id` → `FIREBASE_PRIVATE_KEY_ID`, `private_key` → `FIREBASE_PRIVATE_KEY`, `client_email` → `FIREBASE_CLIENT_EMAIL`, `client_id` → `FIREBASE_CLIENT_ID`, `client_x509_cert_url` → `FIREBASE_CLIENT_CERT_URL` |
| `FIREBASE_DATABASE_URL` | Firebase console → **Realtime Database** → the URL at the top |
| `EMAIL_ADDRESS`, `EMAIL_PASSWORD` | your Gmail address and the 16-letter **App Password** (not your normal password) |
| `ALLOWED_EMAIL_DOMAINS` | e.g. `temple.edu`: only these emails can sign in as instructor. **Leave it empty and anyone can.** |
| `REDIS_URL` | Upstash → your database → the `rediss://…` URL (leave empty for step 4) |

**`FIREBASE_PRIVATE_KEY`:** paste it as **one line**, keeping the `\n` characters exactly as
they appear in the JSON, without the surrounding quotes. It starts with
`-----BEGIN PRIVATE KEY-----\n`.

`.env` stays on your computer only. It is never pushed to git or put in the image.

## Step 4: Test on your computer

```
docker compose up -d --build
```

The first build takes a few minutes. Then open:

- <http://localhost:8000>: the site
- <http://localhost:8000/health>: should show `"status": "healthy"`, `"firebase_status": "connected"`,
  `"session_store": "redis"`, `"code_runner": "service"`
- <http://localhost:8000/instructor>: sign in with your email; the code arrives by email

Open a quiz as a student and press **Run** once to check that code runs. Stop everything with:

```
docker compose down
```

## Step 5: Publish the image to Docker Hub

```
docker login
./release.sh
```

`docker login` asks for your Docker Hub username and password (or an access token). The release
builds the image and pushes **`rahadarmannabid/distractors:v1.0.0`** (each later release
counts up: `v1.0.1`, `v1.0.2`, …). The first build takes 10–20 minutes; later ones are faster.

## Step 6: Set up Koyeb (one time)

### 6a. Store your secrets

Koyeb → **Secrets** → **Create secret**, once for each of these, using the values from your `.env`:

`OPENAI_API_KEY`, `SESSION_SECRET_KEY`, `RUNNER_TOKEN`, `FIREBASE_PRIVATE_KEY`, `EMAIL_PASSWORD`, `REDIS_URL`

### 6b. Create the runner service (private)

Koyeb → **Create Service** → **Docker image**:

| Setting | Value |
|---------|-------|
| Image | `docker.io/rahadarmannabid/distractors:v1.0.0` |
| Private registry secret | only if your Docker Hub repo is private |
| **Command** (override) | `uvicorn runner:app --host 0.0.0.0 --port 8100 --log-level warning` |
| App name | e.g. `distractors` (the web service goes in the same app) |
| Service name | **`runner`** |
| Instance | 2 GB RAM or more |
| Port | `8100`, protocol HTTP, **not public** (no route) |
| Health check | HTTP, port `8100`, path `/health` |
| Environment variables | `RUNNER_TOKEN` = secret `RUNNER_TOKEN`; `CODE_MAX_CONCURRENT` = `8` on a 2 GB instance (about 256 MB per running program) |

Add **only** these variables. The runner must not get your other secrets.

### 6c. Create the web service (public)

Same app → **Create Service** → **Docker image**, same image:

| Setting | Value |
|---------|-------|
| Service name | `web` |
| Command | leave empty (the image's default) |
| Instance | 2 GB RAM or more |
| Port | `8000`, protocol HTTP, public route `/` |
| Health check | HTTP, port `8000`, path `/health`, grace period 60 s |

Environment variables: every line of your `.env` except `API_HOST`, `API_PORT`, `API_RELOAD`,
`SUBMISSIONS_DIR`, `TEACHER_ACCESS_CODE`. Use the **secrets** from 6a for the sensitive ones, and add:

| Variable | Value |
|----------|-------|
| `RUNNER_URL` | `http://runner:8100` |
| `RUNNER_TOKEN` | secret `RUNNER_TOKEN` (the same one as the runner) |
| `REDIS_URL` | secret `REDIS_URL` |

Click **Deploy** and wait until it says **Healthy**.

### 6d. Check it

Open `https://<your-app>.koyeb.app/health`. You should see `"status": "healthy"`, `"version": "v1.0.0"`,
`"firebase_status": "connected"` and `"session_store": "redis"`. Then sign in at `/instructor` and
run a program as a student.

If running code fails, change `RUNNER_URL` to `http://runner.<app-name>.koyeb:8100` and redeploy.

## Step 7: Every time you change the code

```
./release.sh --koyeb distractors/web --koyeb distractors/runner
```

(replace `distractors` with your Koyeb app name; run `koyeb login` once first). This publishes the next
version and moves both services to it. Koyeb switches over only after the new version passes its
health check, so the site stays up.

Without the Koyeb CLI: run `./release.sh`, then in Koyeb open each service →
**Settings** → change the image tag to the new version → **Deploy**.

Then commit the updated `VERSION` file:

```
git add VERSION
git commit -m "Release v1.0.1"
```

## Rolling back

Something broke after a release? Point the services at the previous tag:

```
koyeb service update distractors/web --docker rahadarmannabid/distractors:v1.0.0
koyeb service update distractors/runner --docker rahadarmannabid/distractors:v1.0.0
```

(or change the tag in each service's settings in the dashboard).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Docker is not running` | open Docker Desktop and wait until it says it is running |
| `denied: requested access to the resource is denied` | `docker login`, and check the Docker Hub repo `distractors` exists |
| `/health` shows `"firebase_status": "disconnected"` | a `FIREBASE_*` value is wrong, most often `FIREBASE_PRIVATE_KEY` (one line, keep the `\n`) |
| Instructor code email never arrives | `EMAIL_ADDRESS` / `EMAIL_PASSWORD` wrong; the password must be a Gmail **App Password** |
| "Email not allowed" at instructor sign-in | add the domain to `ALLOWED_EMAIL_DOMAINS` |
| `"session_store": "memory"` | `REDIS_URL` missing or wrong; the site then runs 1 worker, which is fine for small classes |
| Students' code never runs | check `RUNNER_URL` and that `RUNNER_TOKEN` is identical on both services; read the runner's logs |
| Koyeb health check fails | open the service → **Logs**; usually a missing variable |

## Good to know

- **Images instructors upload into survey questions** are saved on the server's disk, which Koyeb
  wipes on every deploy. None of your quizzes use uploaded images yet; until uploads are stored in
  Firebase, re-upload any such image after each release.
- More details (Docker Compose, release options, sizing): [DOCKER.md](./DOCKER.md).
