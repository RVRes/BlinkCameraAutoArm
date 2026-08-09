# BlinkCameraAutoArm

Automatically arms and disarms your Blink security cameras based on whether
anyone's home — detected by pinging IP addresses on your local network (e.g.
phones, laptops). No one's phone responding to ping? Cameras arm themselves.
Someone's home? Cameras disarm. Runs unattended as a daemon, with a Telegram
bot for manual control, live status, and on-demand snapshots/clips.

Originally built to run on a Keenetic router (Entware/OPKG), but works on any
machine with Python 3.10+.

## How it works

1. A background loop periodically pings a list of IP addresses (e.g. everyone
   in the household's phone). Presence uses a sliding-window check — a single
   dropped ping won't falsely trigger arming.
2. If **none** of the monitored IPs respond for several consecutive checks,
   the app arms motion detection on your configured cameras and sends you a
   Telegram notification.
3. As soon as **any** monitored IP responds again, cameras are disarmed and
   you're notified.
4. A Telegram bot lets you check status, manage which IPs/cameras are
   monitored, pull snapshots or clips on demand, and enable/disable
   automatic arming — all gated to a single authorized Telegram user.

## Features

- **Multi-camera support** — arm/disarm any subset of the cameras on your
  Blink account independently of the others.
- **Telegram bot control** — single `/camerabot` command with sub-commands
  for status, enabling/disabling, managing monitored IPs and cameras,
  snapshots, clips, and motion alerts.
- **2FA support** — Blink's OAuth2 login flow (including two-factor
  authentication) is driven entirely through Telegram; no interactive
  terminal prompt required.
- **Persistent session** — once logged in, credentials are cached so the app
  doesn't need to re-authenticate (or re-trigger 2FA) on every restart.
- **On-demand media** — pull a live snapshot or the latest motion clip from
  any camera at any time via Telegram, independent of the auto-arm state.
- **Optional motion alerts** — get proactively notified in Telegram whenever
  a controlled camera detects motion, with the clip attached.
- **Runtime-configurable** — monitored IPs, controlled cameras, ping
  interval, and motion-alert toggle are all managed live via Telegram
  commands and persisted to `config.json`; no restart needed.
- **Resilient by design** — a Telegram outage, a single failed Blink API
  call, or a transient network drop never crashes the app or permanently
  disables auto-arming.

## Requirements

- Python 3.10+
- A Blink account with at least one camera
- A Telegram bot (create one via [@BotFather](https://t.me/BotFather)) and a
  chat/group for it to operate in

## Setup

```bash
# Clone and enter the project
git clone <this-repo>
cd BlinkCameraAutoArm

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
.venv\Scripts\activate             # Windows

# Install dependencies
pip install -r requirements.txt

# Configure secrets
cp .env_example .env
```

Edit `.env` with your real values:

| Variable | Description |
|---|---|
| `BLINK_CAMERA_AUTO_ARM_USERNAME` | Blink account email |
| `BLINK_CAMERA_AUTO_ARM_PASSWORD` | Blink account password |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ID of the chat/group the bot will operate in |
| `TELEGRAM_ALLOWED_USER_ID` | Your personal Telegram user ID — only this user's commands are obeyed |
| `LOG_LEVEL` | Optional; `DEBUG`, `INFO` (default), or `WARNING` |

Find your Telegram user ID and chat ID by messaging
[@userinfobot](https://t.me/userinfobot).

## Running

```bash
python blink_camera_auto_arm.py
```

On first run:

1. `config.json` is created with empty defaults — no IPs monitored, no
   cameras controlled yet (see the configuration reference below).
2. The app attempts to log in to Blink. If your account requires two-factor
   authentication, you'll get a Telegram message asking for the code Blink
   sent you — reply with `/camerabot 2fa <code>`.
3. Once connected, run `/camerabot ips add <ip>` for each device (e.g. phone,
   laptop) whose presence should keep the cameras disarmed, then
   `/camerabot cameras list` to see every camera on your account, and
   `/camerabot cameras add <name>` for each one you want auto-armed.
4. Run `/camerabot status` to confirm everything looks right — app enabled,
   IPs being monitored, cameras controlled.

**Monitored IPs and controlled cameras are managed only through Telegram
commands** (`/camerabot ips add|remove`, `/camerabot cameras add|remove`) —
never by editing `config.json` directly while the app is running, since any
external edit would be overwritten by the next Telegram-triggered save.

## Telegram commands

All commands are sub-commands of `/camerabot`, and only work for the user ID
configured as `TELEGRAM_ALLOWED_USER_ID`, sent in the configured chat.

| Command | Description |
|---|---|
| `help` | List all available commands |
| `status` | Show app state, IP presence, and camera status |
| `enable` / `disable` | Turn automatic arming on/off |
| `cameras list` | List every camera on the Blink account |
| `cameras add <name>` | Add a camera to the auto-arm set |
| `cameras remove <name>` | Remove a camera from the auto-arm set |
| `ips list` | List monitored IPs and their last-known presence |
| `ips add <ip>` | Add an IP address to monitor |
| `ips remove <ip>` | Stop monitoring an IP address |
| `2fa <code>` | Submit a Blink two-factor authentication code |
| `snapshot <name>` | Take and send a live snapshot from a camera |
| `clip <name>` | Send the most recent motion clip from a camera |
| `alerts on` / `alerts off` | Toggle proactive motion-detection alerts |
| `settings show` | Show current `ping_interval_seconds` and `absence_checks` |
| `settings ping_interval <seconds>` | Change how often the main loop runs |
| `settings absence_checks <count>` | Change how many consecutive failed pings mean "away" |

Camera names may contain spaces (e.g. `/camerabot cameras add Front Door`).

## Configuration reference (`config.json`)

Created automatically (with empty defaults) on first run; thereafter
managed exclusively via Telegram commands — never edit this file by hand
while the app is running.

| Field | Description | Default |
|---|---|---|
| `monitored_ips` | IPs checked for presence | `[]` — add via `/camerabot ips add <ip>` |
| `controlled_cameras` | Camera names included in auto-arm/disarm | `[]` — add via `/camerabot cameras add <name>` |
| `absence_checks` | Consecutive failed pings before considered "away" | `5` |
| `ping_interval_seconds` | How often the main loop runs | `60` |
| `motion_alerts_enabled` | Send Telegram alerts on motion detection | `false` |

## Development

Install dev dependencies (adds black, ruff, pytest on top of the runtime
requirements):

```bash
pip install -r requirements-dev.txt
```

Run the test suite (all external I/O — Blink API, Telegram, ping — is
mocked; filesystem access in config/credential tests is isolated to a
per-test temporary directory via pytest's `tmp_path` fixture rather than
mocked outright. No real network calls or credentials are required):

```bash
pytest -v
```

Format and lint before committing:

```bash
black --check . && ruff check . && pytest -v
```

See `AGENTS.md` for detailed code style and architecture conventions.

## Deployment (Keenetic router / Entware)

The app is designed to run as a daemon on an embedded router via Entware's
OPKG package manager, supervised by cron since there's no proper init
system for long-running user services:

1. Copy the project to `/opt/scripts/blink_auto_arm` on the router (the
   watchdog script hardcodes this path — update `PROJECT_PATH` at the top
   if you deploy elsewhere) and set up a venv there as above.
2. Install the `S05crond` script into `/opt/etc/init.d/` to ensure `crond`
   starts on boot.
3. Add a cron entry to run `check_running_blink_auto_arm.sh` every minute —
   it starts the app if it isn't already running:
   ```
   * * * * * sh /opt/scripts/blink_auto_arm/check_running_blink_auto_arm.sh > /dev/null 2>&1 &
   ```
4. Application logs go to stdout, which the watchdog script pipes to the
   router's syslog via `logger`.

The watchdog script checks the app's liveness via a PID file (next to the
app, dotfile-prefixed) before touching anything else — no `mkdir` lock or
other filesystem write happens on a tick where the app is already
confirmed running, to minimize write cycles on the router's flash storage.
The lock directory is only created in the rarer case where a start
decision is actually needed — see the comments in
`check_running_blink_auto_arm.sh` for details. These files are
created/removed automatically; no setup step is required for them.

## License

MIT — see `LICENSE.md`.
