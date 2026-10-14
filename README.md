# CodeOracle / AI Code Assistant

[![Python](https://img.shields.io/badge/python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/OpenAI_API-required-412991?style=flat&logo=openai&logoColor=white)](https://platform.openai.com/)
[![License](https://img.shields.io/badge/license-Project%20local-lightgrey?style=flat)](.)
[![Uvicorn](https://img.shields.io/badge/uvicorn-async-0.24-4488CC?style=flat)](https://www.uvicorn.org/)

Web-based coding environment for research studies: participants write code in a Monaco editor while the backend calls OpenAI to infer intent and suggest the next line (GitHub Copilot–style). Sessions, submissions, surveys, and activity can be persisted to **Firebase Realtime Database** when configured.

**Author:** Rahad Arman Nabid · *PhD Student, Temple University*

---

## What it does

| Area | Functionality |
|------|----------------|
| **IDE** | Multi-file tree, Monaco editor, font controls, **interactive terminal** (live output, typing input for `input()` / `scanf` / `cin` / `Scanner`, Stop) with Run / Clear / Submit. Python, C (gcc), C++ (g++, C++17) and Java (javac + java) compile and run in a sandboxed runner; JavaScript runs in a browser worker |
| **AI assist** | Condition 2: inline **ghost-text** suggestions (single or multi-line) as you type, `Tab` accept / `Esc` dismiss. Condition 3: **Cursor-style agent** chat with Ask / Agent / Plan / Debug modes that reads, edits, creates, deletes and runs files, with per-file Keep/Undo review and skills (instructor- and student-defined). Logic lives in `ai_assist.py` and `templates/ai_modes.js` |
| **Conditions** | **1 = No AI**, **2 = code suggestion** (ghost text), **3 = AI agent**. Instructors pick one or more per quiz (students are randomly assigned); legacy assignment keys map via `CONDITION_*_PREFIXES` |
| **Problem panel** | Assignment text, **AI history** tab with newest-first pagination (5 + “See more”) |
| **Surveys** | Pre/post/periodic surveys (content configurable via env and Firebase-backed quiz config where used) |
| **Persistence** | Code submissions and logs under configurable dirs; optional **Firebase** for live session/checkpoint data |
| **Admin / instructor** | Separate routers: `/admin` (admin UI/API), `/instructor` (quiz authoring, analytics) |

---

## Quick start

### 1. Clone and Python environment

```bash
git clone <your-repo-url>
cd autosuggestion-quiz-v4
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file in the project root (same folder as `main.py`). **Minimum** for local dev:

```env
OPENAI_API_KEY=sk-...
APP_TITLE=AI Code Assistant API
API_HOST=0.0.0.0
API_PORT=8000
```

Optional but common:

| Variable | Purpose |
|----------|---------|
| `API_RELOAD` | `true` / `false` — auto-reload (default `true`) |
| `LOG_LEVEL` | e.g. `info` |
| `SESSION_SECRET_KEY` | Session signing — **set a strong value in production** |
| `CORS_ORIGINS` | Comma-separated origins or `*` (default) |
| `SUBMISSIONS_DIR` | Where submissions/logs go (default `submissions`) |
| `CONDITION_1_PREFIXES`, `CONDITION_2_PREFIXES`, `CONDITION_3_PREFIXES` | Optional, legacy: assignment keys → study condition. Unset = only instructor quiz links work |
| `MAX_CODE_LENGTH`, `MIN_CODE_CHARS_FOR_SUGGESTION` | Editor / suggestion limits |
| `COMPLETION_MODEL`, `AGENT_MODEL`, `AGENT_MAX_STEPS` | Ghost-text model (default `gpt-4.1-nano`), agent model (default `gpt-4.1-mini`), max agent tool steps per message (default 15) |
| `RUNNER_URL`, `RUNNER_TOKEN` | Address and shared secret of the separate code-runner service (`runner.py`). Unset = run code locally (development). See [DOCKER.md](./DOCKER.md#student-code-runner-required-in-production) |
| `CODE_MAX_CONCURRENT`, `CODE_RUNS_PER_MINUTE`, `CODE_CPU_SECONDS`, `CODE_WALL_SECONDS`, `CODE_IDLE_SECONDS`, `CODE_MEMORY_MB`, `CODE_OUTPUT_BYTES` | Terminal guardrails (defaults 40, 20, 10 s, 120 s, 60 s, 256 MB, 100 KB) — see `sandbox.py` |

**Firebase (optional)** — if omitted, the app runs without cloud DB; local features still work.

- Either set `GOOGLE_APPLICATION_CREDENTIALS` to a service account JSON path **and** `FIREBASE_DATABASE_URL`,  
- Or set individual `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`, etc. (see `initialize_firebase()` in `main.py`).

**Email (optional)** — `EMAIL_ENABLED=true` plus `SMTP_SERVER`, `SMTP_PORT`, `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `NOTIFICATION_EMAIL` for notifications where implemented.

### 3. Run

```bash
python main.py
```

- App: `http://localhost:8000` (or your `API_PORT`)
- OpenAPI docs: `http://localhost:8000/docs`
- Admin area: `/admin` (configure as deployed)
- Instructor area: `/instructor`

### Docker Hub

Container image **`rahadarmannabid/codeoracle`** (`build-and-push.sh` publishes **amd64 + arm64** for hosts like Koyeb). Details: **[DOCKER.md](./DOCKER.md)**.

---

## Project layout

```
├── Dockerfile           # Container build (Hub-safe: no .env in image)
├── DOCKER.md            # Push to Docker Hub, run on server
├── main.py              # FastAPI app, AI routes, editor API, surveys
├── admin.py             # Router: /admin
├── instructor.py        # Router: /instructor
├── firebase_utils.py    # Firebase helpers
├── models.py, otp.py, quiz_analytics.py
├── requirements.txt
├── templates/           # Jinja HTML (index IDE, home, surveys, instructor UIs)
├── submissions/       # Default output for submissions (configurable)
├── scripts/           # Utilities (see Load testing below)
└── starter_code/      # Default starter files dir (configurable via STARTER_CODE_DIR)
```

---

## Load testing (optional)

`scripts/emulate_students_loadtest.py` hammers the same HTTP endpoints the IDE uses after a quiz join (`/health`, `/quiz/{code}`, `/quiz/join`, `/log-activities`, `/checkpoint-editor`). Requires `httpx` (already in `requirements.txt`).

```bash
# Local — set LOADTEST_QUIZ_CODE to your instructor share code
LOADTEST_BASE_URL=http://127.0.0.1:8000 LOADTEST_QUIZ_CODE=YOUR_CODE python scripts/emulate_students_loadtest.py

# Remote host — use only when you intend to stress production
LOADTEST_BASE_URL=https://www.guide.codes LOADTEST_QUIZ_CODE=YOUR_CODE python scripts/emulate_students_loadtest.py
```

Important: `LOADTEST_QUIZ_LANGUAGE` must match the quiz’s **allowed** languages (default is `python`). Transient network errors retry once; exit code `0` means no 5xx and no failed transport on the sampled requests. Set `LOADTEST_ANALYZE=1` only if you want to include `POST /analyze` (uses OpenAI quota).

---

## Requirements

- **Python 3.10+** recommended (3.8+ may work; match your deployment)
- **OpenAI API key** with access to the model configured in code
- Modern browser (Chromium / Firefox / Safari)
- **Firebase** only if you need cloud session storage and related features

---

## Keyboard shortcuts (IDE)

| Shortcut | Action |
|----------|--------|
| `Tab` | Accept AI suggestion (when popup focused) |
| `Esc` | Reject suggestion |
| `Ctrl+Enter` / `F5` | Run code (where bound in UI) |
| `Ctrl+Shift+C` | Clear terminal |
| `Ctrl+Shift+S` | Submit |

---

## Troubleshooting

- **No suggestions** — Check `OPENAI_API_KEY`, network, and browser console / server logs.
- **Firebase disconnected** — Expected if env vars are unset; app still runs for local testing.
- **Port in use** — Change `API_PORT` in `.env`.

---

## Citation / contact

If you reuse this system in research, cite or credit the authors and repository as appropriate for your IRB or publication. For setup questions, open an issue on your fork or contact the maintainer.
