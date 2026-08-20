#!/bin/sh
# Cron watchdog: starts the app if it is not already running, logging
# output via the router's syslog (logger).
#
# Put this task in crontab (crontab -e):
#   * * * * * sh /opt/scripts/blink_auto_arm/check_running_blink_auto_arm.sh > /dev/null 2>&1 &
# Add S05crond to /opt/etc/init.d/ to ensure crond itself autostarts.
#
# Write-cycle minimization: on a normal tick where the app is already
# running, this script performs a read-only liveness check (read the PID
# file, `kill -0` it) and exits — no filesystem write of any kind, since
# the router's storage is flash and every avoidable write/erase cycle
# matters. The `mkdir` lock (below) is only taken on the rarer path where
# a start actually looks necessary.
#
# Locking: overlapping cron invocations (e.g. a slow
# prior run still executing when the next minute's invocation fires) are
# serialized with an atomic `mkdir` lock — the classic portable idiom for
# POSIX sh/busybox ash, which has no `flock` builtin. Liveness of the app
# itself is tracked via a PID file written by this script (not fragile
# `ps | grep` pattern matching), so a start is only skipped if that exact
# PID is still alive. The liveness check is re-done immediately after
# acquiring the lock, since another invocation may have started the app
# between our first (lock-free) check and taking the lock.

PROJECT_PATH="/opt/scripts/blink_auto_arm"
APP="blink_camera_auto_arm"
PYTHON="$PROJECT_PATH/venv/bin/python"

APP_PATH="$PROJECT_PATH/$APP.py"
PID_FILE="$PROJECT_PATH/.$APP.pid"
LOCK_DIR="$PROJECT_PATH/.$APP.lock"

is_app_alive() {
    # Read-only liveness check — no filesystem writes. Returns success
    # (0) if $PID_FILE names a still-live process.
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

if is_app_alive; then
    # Already running — nothing to do, and nothing was written.
    exit 0
fi

# App looks like it might need starting. Take the mkdir lock (a real
# filesystem write) only now, to serialize against any other invocation
# reaching this same point concurrently.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # Another invocation of this watchdog is currently checking/starting
    # the app — do nothing rather than racing it.
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM

# Re-check under the lock: another invocation may have started the app
# between our first check (above) and acquiring the lock just now.
if is_app_alive; then
    exit 0
fi

echo "$APP_PATH is not running. Starting it now." | logger -t "$APP"

# Run the app in a subshell so we can capture its real PID (the `wait`
# keeps this subshell alive until the app exits) while still piping its
# combined stdout/stderr to syslog via logger. Capturing $! directly on
# `cmd | logger &` would give logger's PID, not the app's.
(
    "$PYTHON" -u "$APP_PATH" &
    echo "$!" > "$PID_FILE"
    wait
) 2>&1 | logger -t "$APP" &
