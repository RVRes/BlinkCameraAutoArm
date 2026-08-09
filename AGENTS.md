# AGENTS.md — BlinkCameraAutoArm

Guidance for agentic coding agents operating in this repository.

---

## Project Overview

**BlinkCameraAutoArm** is a Python asyncio application that automatically arms/disarms one or
more Blink security cameras based on whether specific IP addresses on the local network respond
to ping. It runs as a cron-watched daemon on a Keenetic router (Entware OPKG / embedded Linux)
and exposes a Telegram bot interface for manual control, status queries, and on-demand snapshots
and clips.

**Entry point:** `blink_camera_auto_arm.py` → `asyncio.run(main())`
**Runtime:** Python 3.10+ (targeted `py310`; deployed under Python 3.14 CPython)
**Architecture:** Flat module structure — no packages, no `__init__.py`, all source files at root.

`PLAN.md` and `STATUS.md` are gitignored, local-only planning/progress notes — they may not exist
in every checkout. If present, `PLAN.md` has the full design rationale (root causes of the
pre-rewrite breakage, module contracts, authentication flow, and test strategy) for historical
context beyond what's summarized here, and `STATUS.md` tracks implementation progress across
sessions.

---

## Environment Setup

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
.venv\Scripts\activate             # Windows

# Install runtime + dev dependencies
pip install -r requirements-dev.txt

# For a reproducible install matching the exact versions this project
# was last tested against (recommended before deploying to the router;
# see constraints.txt and the Dependency Management notes below):
pip install -r requirements-dev.txt -c constraints.txt

# Configure secrets
cp .env_example .env
# Edit .env with real Blink credentials, Telegram token/chat ID, allowed user id
```

Required `.env` variables (see `.env_example`):
- `BLINK_CAMERA_AUTO_ARM_USERNAME` — Blink account email
- `BLINK_CAMERA_AUTO_ARM_PASSWORD` — Blink account password
- `TELEGRAM_BOT_TOKEN` — Telegram HTTP API token
- `TELEGRAM_CHAT_ID` — target group chat ID
- `TELEGRAM_ALLOWED_USER_ID` — the only Telegram user ID whose commands are obeyed
- `LOG_LEVEL` — optional, defaults to `INFO`

Mutable runtime configuration (monitored IPs, controlled cameras, intervals, motion-alert
toggle) lives in `config.json`, created with empty defaults on first run and thereafter managed
via Telegram commands (see `README.md`). **Never edit `.env` to change monitored IPs or cameras
after first run** — use `/camerabot ips add|remove` and `/camerabot cameras add|remove` instead;
manual `.env` edits are ignored once `config.json` exists.

---

## Running the Application

```bash
python blink_camera_auto_arm.py
```

On the deployment target (Keenetic router), the application is managed by a cron watchdog:

```bash
# Watchdog script — checks if the process is running and starts it if not
sh check_running_blink_auto_arm.sh
```

Crond autostart service (Entware): `S05crond` → place in `/opt/etc/init.d/`.

These two deployment scripts reference `blink_camera_auto_arm.py` by filename only and require
no changes when application internals change — only touch them if the entry-point filename or
the router's venv path changes. The watchdog script serializes overlapping cron invocations with
an atomic `mkdir` lock (only taken on the rarer not-yet-confirmed-running path, not on every
tick — see the script's own comments) and tracks the app's liveness via a PID file (rather than
fragile `ps | grep` pattern matching); `S05crond` similarly tracks its own crond PID instead of
using `killall crond`, which could otherwise terminate unrelated cron instances on the router.

---

## Build / Lint / Test Commands

### Formatting (Black)

`pyproject.toml` configures Black (and ruff) with an 80-character line length override.

```bash
black .                    # Format all source files
black --check .            # Check formatting without modifying files
black config.py            # Format a single file
```

**Line length override:** this project uses **80 characters**, not Black's 88-char default —
see `pyproject.toml`'s `[tool.black]` `line-length = 80`.

### Linting

**ruff** is configured via `pyproject.toml` (`[tool.ruff]` / `[tool.ruff.lint]`), matching
Black's 80-character line length, rule set `E, F, W, I, UP, B, C4, SIM`. `E501` (line-too-long)
is intentionally enabled — it catches long lines Black cannot safely auto-wrap (long strings,
comments). Run `ruff check .` before every commit. Do not introduce linting rules that conflict
with Black's formatting.

### Type Checking

No mypy configuration exists. If you add mypy, annotate only new code — do not refactor existing
partial annotations in a single pass.

### Tests

Full **pytest** suite lives in `tests/`, covering every module (`state.py`, `config.py`,
`presence_monitor.py`, `blink_service.py`, `telegram_bot.py`, and the main loop in
`blink_camera_auto_arm.py`). All external I/O (blinkpy, Telegram, ping/subprocess, file writes)
is mocked — tests never touch the network or the filesystem outside `tmp_path` fixtures.

```bash
pytest                                              # Run all tests
pytest tests/test_blink_service.py                  # Run a single test file
pytest tests/test_blink_service.py::test_arm_cameras_calls_async_arm_true  # Single test
pytest -s                                            # Disable output capture (async debugging)
```

Place new test files in `tests/`, named `test_<module>.py`, mirroring the module under test.

`test_task.py` at the project root is **not** a pytest file and is unrelated to the test suite —
it's a manual cron/logging smoke-test script (infinite loop with periodic prints), used to
verify OPKG/router syslog integration by running it under cron and checking router logs. Do not
add it to `tests/` or confuse it with actual test coverage.

Run the full gate before any commit:

```bash
black --check . && ruff check . && pytest -v
```

---

## Code Style Guidelines

### Language and Formatting

- **Python 3.10+ syntax required.** Use `match/case`, `X | Y` union syntax, and PEP 604 type
  unions freely — this is the baseline, not merely "acceptable."
- **Black is the authority on formatting.** Never manually adjust whitespace, quotes, or trailing
  commas — run `black .` after any edit.
- **4-space indentation**, no tabs.
- **Double quotes** for all strings (Black enforces this).
- **f-strings** for all string interpolation — never `%` formatting or `.format()`.
- Line length: **80 characters** (project override in `pyproject.toml`).

### Imports

Order imports in three groups separated by a blank line (PEP 8):

```python
# 1. Standard library
import asyncio
import logging
from dataclasses import dataclass, field

# 2. Third-party
from blinkpy.blinkpy import Blink
from dotenv import load_dotenv

# 3. Local modules
from blink_service import BlinkService
from config import AppConfig, Config
from state import AppState
```

- Use bare module-level imports only — this is a flat project with no package hierarchy.
- No relative imports (e.g., no `from . import ...`).
- `ruff`'s `I` rule enforces import sorting — run `ruff check --fix .` if it flags ordering.

### Naming Conventions

| Item | Style | Example |
|---|---|---|
| Functions / methods | `snake_case` | `arm_cameras`, `run_iteration` |
| Variables | `snake_case` | `camera_instance`, `ping_results` |
| Classes | `PascalCase` | `BlinkService`, `PresenceMonitor`, `AppState` |
| Constants / class-level config | `UPPER_SNAKE_CASE` | `CREDENTIALS_FILE`, `BOT_INVOCATION_COMMAND` |
| Private methods / attributes | `_single_leading_underscore` | `_is_authorized`, `self._blink` |
| Environment variable names | `UPPER_SNAKE_CASE` | `BLINK_CAMERA_AUTO_ARM_USERNAME` |

### Type Annotations

- **Annotate return types** on all functions: `-> None`, `-> bool`, `-> str`, etc.
- **Annotate parameters** where the type is not obvious from context.
- Use **Python 3.10+ union syntax**: `str | None`, not `Optional[str]`.
- Use **built-in generics**: `list[str]`, `dict[str, int]`, not `List[str]`, `Dict[str, int]`
  from `typing`.

### Error Handling

- Use the standard `logging` module (`logging.getLogger(__name__)` per module), **not**
  `print()`. `logging.basicConfig()` is configured once in `blink_camera_auto_arm.py`'s `main()`,
  writing to stdout (piped to syslog/file by the unmodified watchdog scripts).
- **Catch specific exception types** where the failure mode is known (e.g. `ValueError` for
  malformed config/IP input). Broad `except Exception:` is acceptable — and used deliberately —
  at task-boundary points where a single failure must not crash the whole app (main loop
  iteration, proactive Telegram sends); always `logger.exception(...)` when doing so, never
  swallow silently.
- For missing required configuration, raise `ValueError` with a descriptive message (see
  `config.py`'s `_load_required_env`).
- Never let a Telegram outage or a single Blink API failure crash the main loop — every
  task-boundary point catches broadly and logs, rather than propagating.

### Async Patterns

- All I/O-bound operations must be `async`/`await`. Never use `time.sleep()` in async
  context — use `await asyncio.sleep()`.
- Blocking calls that have no async equivalent (e.g. `subprocess.call()` for ping) must be
  wrapped in `loop.run_in_executor(None, ...)` — see `presence_monitor.py`'s `_ping_async`.
- The main loop and the Telegram bot's polling loop run concurrently as sibling tasks created
  with `asyncio.create_task()` in `main()`, coordinated via `asyncio.wait(...,
  return_when=asyncio.FIRST_COMPLETED)` so that either one finishing (e.g. on shutdown) triggers
  cleanup of the other; `asyncio.gather()` is used only to drain both tasks during that cleanup.
- The application has a single event loop started with `asyncio.run(main())`.
- Blink API calls (`blinkpy`) are async — always `await` them.

### State Management

Runtime cross-task state lives in `AppState` (`state.py`), a plain mutable dataclass —
**instantiated once** in `main()` and passed by reference to every module that needs it
(`TelegramBot`, the main loop). There is no class-level singleton pattern and no global
variables. This is safe without locks because asyncio is single-threaded — concurrent tasks only
interleave at `await` points, never truly in parallel.

```python
# Correct — instantiate once, pass by reference
state = AppState()
bot = TelegramBot(..., state=state, ...)
await run_main_loop(..., state=state, ...)
```

Persistent mutable configuration (monitored IPs, controlled cameras, intervals, motion-alert
toggle) is owned by `AppConfig`/`Config` (`config.py`) and written atomically to `config.json`.
Secrets (Blink credentials, Telegram token/chat/user IDs) are owned by `.env` only and never
written to `config.json`.

### Architecture Conventions

- **Keep the flat structure.** Do not introduce packages or subdirectories for source code
  unless the project grows substantially.
- **One class per file**, matching module name: `BlinkService` in `blink_service.py`,
  `TelegramBot` in `telegram_bot.py`, `PresenceMonitor` in `presence_monitor.py`, `Config`/
  `AppConfig` in `config.py`, `AppState` in `state.py`.
- **`python-telegram-bot` v21 Application builder pattern** — register handlers via
  `application.add_handler(CommandHandler(...))`. All commands are routed through a single
  `/camerabot <noun> [verb] [args...]` entry point in `TelegramBot.handle_command`.
- **Every Telegram handler is authorization-gated** via `TelegramBot._is_authorized()` — checks
  both `chat_id` and `allowed_user_id` from `.env`. Never add a handler that skips this check.

---

## Key Files

| File | Purpose |
|---|---|
| `blink_camera_auto_arm.py` | Entry point; wires up config/state/services and runs the main loop + Telegram bot concurrently |
| `config.py` | `AppConfig` dataclass + `Config` loader/saver (`.env` secrets + `config.json` mutable state) |
| `state.py` | `AppState` — runtime-only cross-task signals, passed by reference, never persisted |
| `presence_monitor.py` | `PresenceMonitor` — async ping-based IP presence tracking with sliding-window tolerance |
| `blink_service.py` | `BlinkService` — async wrapper around blinkpy (auth/2FA, camera arm/disarm, snapshots, clips, motion events) |
| `telegram_bot.py` | `TelegramBot` — authorization-gated `/camerabot` command router and proactive notification sender |
| `tests/` | Full pytest suite, one `test_<module>.py` per module, all external I/O mocked |
| `config.json` | Gitignored; mutable runtime config (IPs, cameras, intervals) — created with empty defaults on first run, managed via Telegram commands thereafter |
| `requirements.txt` | Pinned runtime dependencies (deployed to the router) |
| `requirements-dev.txt` | Adds black/ruff/pytest/pytest-asyncio on top of `requirements.txt` — dev machine only |
| `constraints.txt` | Exact-version pins for the full runtime+dev dependency tree (direct and transitive) — apply with `pip install -r requirements-dev.txt -c constraints.txt` for a reproducible install; regenerate deliberately after testing an upgrade |
| `.env_example` | Template for secrets — copy to `.env` |
| `blink_credentials.json` | Gitignored; blinkpy's persisted OAuth2 session (created at runtime, never committed) |
| `test_task.py` | Manual cron/syslog smoke-test script — NOT part of the pytest suite |
| `check_running_blink_auto_arm.sh` | Cron watchdog (syslog output) |
| `S05crond` | Entware crond init service script |
| `PLAN.md` | Gitignored, local-only design document for the rewrite (may not exist in every checkout) — module contracts, auth flow detail, test strategy |

---

## Important Notes for Agents

- **Never commit, read, display, or log the contents of `.env`, `config.json`, or
  `blink_credentials.json`** — all three are gitignored and may contain secrets or home-network
  IP addresses. Whenever spawning a subagent (e.g. via the Task tool), explicitly include this
  restriction in its prompt/instructions — subagents do not inherit this file automatically.
- **Delete `blink_credentials.json` if it predates the OAuth2+PKCE rewrite** (blinkpy ≥0.25) —
  the old schema is incompatible and will cause silent auth failures; blinkpy will just recreate
  it on the next successful login/2FA.
- The `.venv/` directory is present; both `venv` and `.venv` are gitignored — never commit either.
- `blinkpy` is pinned to `>=0.25.8,<0.26` — earlier 0.25.x patches have known arm/disarm and
  OAuth regressions, so don't lower this floor without retesting the auth and arm/disarm flows.
- The deployment target is an **embedded Linux router** with limited resources. Avoid
  introducing heavy dependencies; note `aiohttp>=3.14` (a blinkpy 0.25.8+ transitive requirement)
  may require verifying wheel availability for the router's architecture.
- There is **no CI pipeline**. Validate changes locally: `black --check . && ruff check . &&
  pytest -v`, and manually test against a real `.env` + Blink account when touching
  `blink_service.py` or the auth flow.
