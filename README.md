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
| **IDE** | Multi-file tree, Monaco editor, font controls, terminal with Run / Clear / Submit |
| **AI assist** | Debounced analysis of editor content; floating **Copilot-style popup** with **AI’s interpretation** (conditions 1–2) and **suggested line**; `Tab` accept / `Esc` reject |
| **Conditions** | Assignment key **prefixes** map to study arms: full interpretation + optional “correct” hints (1), interpretation + suggestion (2), **suggestion-only** like Copilot (3) |
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
| `CONDITION_1_PREFIXES`, `CONDITION_2_PREFIXES`, `CONDITION_3_PREFIXES` | Comma-separated assignment key prefixes → study condition |
| `MAX_CODE_LENGTH`, `MIN_CODE_CHARS_FOR_SUGGESTION` | Editor / suggestion limits |

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

---

## Project layout

```
├── main.py              # FastAPI app, AI routes, editor API, surveys
├── admin.py             # Router: /admin
├── instructor.py        # Router: /instructor
├── firebase_utils.py    # Firebase helpers
├── models.py, otp.py, quiz_analytics.py
├── requirements.txt
├── templates/           # Jinja HTML (index IDE, home, surveys, instructor UIs)
├── submissions/       # Default output for submissions (configurable)
└── starter_code/      # Default starter files dir (configurable via STARTER_CODE_DIR)
```

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
