# CodeOracle / AI Code Assistant

[![Python](https://img.shields.io/badge/python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/OpenAI_API-required-412991?style=flat&logo=openai&logoColor=white)](https://platform.openai.com/)
[![License](https://img.shields.io/badge/license-Project%20local-lightgrey?style=flat)](.)
[![Uvicorn](https://img.shields.io/badge/uvicorn-async-0.24-4488CC?style=flat)](https://www.uvicorn.org/)

**Hosting it?** Follow **[HOSTING.md](./HOSTING.md)** step by step.

Web-based coding environment for research studies: participants write code in a Monaco editor while the backend calls OpenAI to infer intent and suggest the next line (GitHub Copilot–style). Sessions, submissions, surveys, and activity can be persisted to **Firebase Realtime Database** when configured.

**Author:** Rahad Arman Nabid · *PhD Student, Temple University*

---

## What it does

| Area | Functionality |
|------|----------------|
| **IDE** | Multi-file tree, Monaco editor, font controls, **interactive terminal** (live output, typing input for `input()` / `scanf` / `cin` / `Scanner`, Stop) with Run / Clear / Submit. Python, C (gcc), C++ (g++, C++17) and Java (javac + java) compile and run in a sandboxed runner; JavaScript runs in a browser worker |
| **AI assist** | Conditions 2 and 4: inline **ghost-text** suggestions as you type (condition 2: one line at a time; condition 4: multi-line blocks like GitHub Copilot), each offered as **two options in random order, one correct and one with a single bug**: `Alt+]` / `Alt+[` switch, `Tab` accept / `Esc` dismiss. Condition 3: **Cursor-style agent** chat with Ask / Agent / Plan / Debug modes that reads, edits, creates, deletes and runs files; its changes are offered as **Version A / Version B** (correct and buggy, random order) and the student applies one, then per-file Keep/Undo review and skills (instructor- and student-defined). Logic lives in `ai_assist.py` and `templates/ai_modes.js` |
| **Conditions** | **1 = No AI**, **2 = single-line code suggestion** (ghost text), **3 = AI agent**, **4 = multi-line code suggestion** (ghost text). Instructors pick one or more per quiz (students are randomly assigned); legacy assignment keys map via `CONDITION_*_PREFIXES` |
| **Problem panel** | Assignment text, **AI history** tab with newest-first pagination (5 + “See more”) |
| **Surveys** | Pre/post/periodic surveys (content configurable via env and Firebase-backed quiz config where used) |
| **Persistence** | Code submissions and logs under configurable dirs; optional **Firebase** for live session/checkpoint data |
| **Instructor** | `/instructor`: quiz authoring and analytics (sign-in with an emailed code) |

---

## Quick start

The easiest path on **any OS (Windows, macOS, Linux)** is Docker: install Docker Desktop, create
`.env` (step 2), then follow **[DOCKER.md → Docker Compose](./DOCKER.md#run-locally-or-on-your-own-server-docker-compose)**.
Everything there is a `docker` or `python` command that is typed the same in every shell.

To run without Docker:

### 1. Clone and Python environment

```
git clone <your-repo-url>
cd <repo-folder>
```

| | macOS / Linux | Windows (PowerShell) | Windows (Command Prompt) |
|---|---|---|---|
| Create venv | `python3 -m venv .venv` | `python -m venv .venv` | `python -m venv .venv` |
| Activate | `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` | `.venv\Scripts\activate.bat` |
| Install | `pip install -r requirements.txt` | `pip install -r requirements.txt` | `pip install -r requirements.txt` |

PowerShell refusing to run `Activate.ps1`: run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
Running C, C++ or Java locally needs `gcc`/`g++` and a JDK on your `PATH` (Docker includes them).
On macOS/Linux, `./start.sh` does steps 1–3 for you.

### 2. Environment variables

Copy `.env.example` to `.env` (same folder as `main.py`) and fill it in; [HOSTING.md](./HOSTING.md#step-3-fill-in-your-settings-env) says where each value comes from. **Minimum** for local dev:

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
| `INSTRUCTOR_LOGIN_TTL_SEC` | How long an instructor stays signed in after entering the emailed code (default 86400 = one day). Students are not affected |
| `CORS_ORIGINS` | Comma-separated origins or `*` (default) |
| `SUBMISSIONS_DIR` | Where submissions/logs go (default `submissions`) |
| `CONDITION_1_PREFIXES`, `CONDITION_2_PREFIXES`, `CONDITION_3_PREFIXES` | Optional, legacy: assignment keys → study condition. Unset = only instructor quiz links work |
| `MAX_CODE_LENGTH`, `MIN_CODE_CHARS_FOR_SUGGESTION` | Editor / suggestion limits |
| `MULTILINE_SUGGESTION_MAX_LINES` | Most lines one condition 4 suggestion may span (default and maximum 5; a higher value is capped at 5) |
| `COMPLETION_MODEL`, `AGENT_MODEL`, `AGENT_MAX_STEPS` | Ghost-text model, which writes both the correct and the buggy option in one call (default `gpt-6-sol` with reasoning off: ~2.3 s per pair and the most realistic bugs; `gpt-4.1` (~1.6 s) and `gpt-4.1-mini` are faster and cheaper but plant unrealistic bugs more often), agent model (default `gpt-6-luna`, with `AGENT_REASONING_EFFORT=low`: ~1.8 s vs ~3 s on gpt-5-nano), max agent tool steps per message (default 15) |
| `OPENAI_MODEL`, `REASONING_EFFORT` | Default model for every AI call (`gpt-5-nano`) and its reasoning effort for the short interactive calls (`minimal`, which keeps latency near 1 s). gpt-4.x models also work; see `llm_params.py` |
| `BUG_MODEL` | Model that writes the buggy version of an agent turn's changes (default `gpt-6-sol`). The bug type is the one of the quiz's selected **bug skills** that best fits the code: the built-in ones are `bug_skills/*.md`, and instructors select, edit, add, upload or remove them per quiz in the quiz form. No bug type selected = a single correct suggestion. Counts per bug type are on the quiz analytics page. See `bug_injection.py` |
| `BUG_CLASSIFIER_MODEL`, `BUG_CLASSIFIER_CONCURRENCY` | Background model that labels each injected bug (default `gpt-5-nano`, 4 at once). Records go to Firebase `bug_injections/{session_id}/{bug_id}` and `$SUBMISSIONS_DIR/bug_injections.jsonl` |
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
- Instructor area: `/instructor`

### Docker Hub and Koyeb

Container image **`rahadarmannabid/distractors`**. Build it on your computer and push a new version (macOS / Linux terminal, or Git Bash on Windows):

```
./release.sh
```

It bumps `VERSION` (`v1.0.3` → `v1.0.4`), builds **amd64 + arm64**, and pushes `:v1.0.4` and `:latest`.
Add `--koyeb <app>/<service>` to deploy it to Koyeb in the same step. Details: **[DOCKER.md](./DOCKER.md)**.

---

## Project layout

```
├── Dockerfile           # Container build (Hub-safe: no .env in image)
├── HOSTING.md           # Step-by-step hosting guide (Docker Hub + Koyeb)
├── DOCKER.md            # Docker / release / Koyeb reference
├── .env.example         # Every setting, with where to get it
├── VERSION              # Last released image version (written by release.sh)
├── main.py              # FastAPI app, AI routes, editor API, surveys
├── instructor.py        # Router: /instructor
├── firebase_utils.py    # Firebase helpers
├── otp.py, quiz_analytics.py
├── requirements.txt
├── bug_skills/          # Built-in bug skills (.md): one bug type each, seeded into every quiz
├── templates/           # Jinja HTML (index IDE, home, surveys, instructor UIs)
├── submissions/       # Default output for submissions (configurable)
├── runner.py, sandbox.py # Isolated code runner for students' programs
├── release.sh         # Build the image and push it to Docker Hub
└── scripts/           # Load tests
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
