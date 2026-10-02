#!/bin/sh
# cc-statusline dashboard, independent of any terminal or agent.
#
# `statusline.py --install` already starts it at login (launchd on macOS,
# systemd on Linux). This script drives that service when there is one, and
# runs the server in the background itself after `--install --no-dashboard`.
#
#   dashboard.sh            start it if needed and open it in the browser
#   dashboard.sh start      start it in the background
#   dashboard.sh stop       stop it (a login service comes back at the next login)
#   dashboard.sh restart
#   dashboard.sh status
#   dashboard.sh logs       follow the server log
#
# CC_STATUSLINE_PORT picks the port (default 8765). The server only listens on
# 127.0.0.1 and reads the database on every request, so it never shows stale
# numbers and can run for weeks.

set -eu

# Resolve symlinks to find the repository.
self=$0
while [ -L "$self" ]; do
    link=$(readlink "$self")
    case $link in
        /*) self=$link ;;
        *) self=$(dirname "$self")/$link ;;
    esac
done
root=$(cd "$(dirname "$self")/.." && pwd -P)

data=${CC_STATUSLINE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cc-statusline}
port=${CC_STATUSLINE_PORT:-8765}
url="http://127.0.0.1:$port/"
pidfile="$data/dashboard.pid"
log="$data/dashboard.log"
label=com.github.lotyszm.cc-statusline.dashboard
plist="$HOME/Library/LaunchAgents/$label.plist"
unit=cc-statusline-dashboard.service

# The interpreter --install recorded for the hooks; python3 from PATH otherwise.
python=python3
if [ -s "$data/python" ]; then
    python=$(cat "$data/python")
fi

# The login service that runs it, if --install set one up.
service=
if [ -f "$plist" ]; then
    service=launchd
elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit" ]; then
    service=systemd
fi

pid() {
    [ -s "$pidfile" ] || return 1
    p=$(cat "$pidfile")
    # A pid from an old run may belong to another process by now.
    ps -p "$p" -o command= 2>/dev/null | grep -q "statusline.py dashboard" || return 1
    echo "$p"
}

answers() {
    curl -s -o /dev/null --max-time 1 "${url}api/report?from=2000-01-01&to=2000-01-01"
}

wait_up() {
    i=0
    while [ $i -lt 50 ]; do
        if answers; then
            echo "dashboard running: $url${service:+ ($service)}"
            return 0
        fi
        i=$((i + 1))
        sleep 0.1
    done
    echo "dashboard did not come up; last lines of $log:" >&2
    tail -5 "$log" >&2 2>/dev/null || true
    return 1
}

start() {
    if answers; then
        echo "dashboard running: $url"
        return 0
    fi
    case $service in
        launchd)
            launchctl kickstart "gui/$(id -u)/$label" 2>/dev/null ||
                launchctl bootstrap "gui/$(id -u)" "$plist"
            ;;
        systemd) systemctl --user start "$unit" ;;
        *)
            mkdir -p "$data"
            nohup "$python" "$root/statusline.py" dashboard --port "$port" --no-open >>"$log" 2>&1 &
            echo $! >"$pidfile"
            ;;
    esac
    wait_up
}

stop() {
    case $service in
        launchd)
            launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
            echo "dashboard stopped; launchd starts it again at the next login"
            ;;
        systemd)
            systemctl --user stop "$unit"
            echo "dashboard stopped; systemd starts it again at the next login"
            ;;
        *)
            if p=$(pid); then
                kill "$p"
                i=0
                while kill -0 "$p" 2>/dev/null && [ $i -lt 30 ]; do
                    i=$((i + 1))
                    sleep 0.1
                done
                echo "dashboard stopped"
            else
                echo "dashboard is not running"
            fi
            rm -f "$pidfile"
            ;;
    esac
}

restart() {
    case $service in
        launchd)
            launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null ||
                launchctl bootstrap "gui/$(id -u)" "$plist"
            wait_up
            ;;
        systemd) systemctl --user restart "$unit" && wait_up ;;
        *) stop && start ;;
    esac
}

status() {
    if answers; then
        echo "running: $url${service:+ (login service: $service)}"
    else
        echo "not running${service:+ (login service: $service)}"
        return 1
    fi
}

open_browser() {
    if command -v open >/dev/null 2>&1; then
        open "$url"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1
    else
        echo "open $url"
    fi
}

case ${1:-} in
    "") start && open_browser ;;
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    logs) mkdir -p "$data" && touch "$log" && tail -f "$log" ;;
    -h | --help | help) sed -n '2,17p' "$self" | sed 's/^# \{0,1\}//' ;;
    *)
        echo "usage: dashboard.sh [start|stop|restart|status|logs]" >&2
        exit 2
        ;;
esac
