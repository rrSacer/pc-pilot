#!/usr/bin/env bash
# Isolated, headless GNOME Shell for testing pcpilot@local (and AT-SPI) without
# ever touching the user's real session.
#
#   dev/headless-shell.sh start [DIR]     start in background, wait until ready, print DIR
#   dev/headless-shell.sh run DIR -- CMD  run CMD as the desktop user inside that session
#   dev/headless-shell.sh env DIR         print the session's environment (KEY=VALUE lines)
#   dev/headless-shell.sh stop DIR        kill the shell and everything launched inside it
#   dev/headless-shell.sh status DIR
#
# The compositor's wayland socket is DIR/runtime/wayland-pcpilot (also exported as
# PCPILOT_WAYLAND_SOCKET in DIR/env) for configuring the real daemon's UI auth.
#
# Isolation: runs as $PCPILOT_TEST_USER (default sacer) inside its own
# dbus-run-session, with a private XDG_RUNTIME_DIR / XDG_{DATA,CONFIG,CACHE,STATE}_HOME
# under DIR and GSETTINGS_BACKEND=keyfile, so neither /run/user/<uid>, dconf nor
# ~/.local/share/gnome-shell are used. The extension is *copied* into DIR at start.
#
# Options (env): PCPILOT_HEADLESS_SIZE (1920x1080), PCPILOT_HEADLESS_LAYOUT (br),
# PCPILOT_HEADLESS_ATSPI=0 to not start at-spi2-registryd,
# PCPILOT_SOCKET (default DIR/pcpilot.sock), PCPILOT_HEADLESS_EXTENSION=0 to not enable
# the extension.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_USER="${PCPILOT_TEST_USER:-sacer}"
SIZE="${PCPILOT_HEADLESS_SIZE:-1920x1080}"
LAYOUT="${PCPILOT_HEADLESS_LAYOUT:-br}"
UUID="pcpilot@local"

die() { echo "headless-shell: $*" >&2; exit 1; }

uid_of() { id -u "$TEST_USER"; }
gid_of() { id -g "$TEST_USER"; }
user_home() { getent passwd "$TEST_USER" | cut -d: -f6; }

# argv prefix that drops to the test user (empty when already that user).
user_prefix() {
    if [[ "$(id -u)" == "$(uid_of)" ]]; then
        :
    elif [[ "$(id -u)" == 0 ]]; then
        printf '%s\0' setpriv --reuid="$(uid_of)" --regid="$(gid_of)" --init-groups --
    else
        die "must run as root or as $TEST_USER"
    fi
}

as_user() {
    local -a pre=()
    while IFS= read -r -d '' x; do pre+=("$x"); done < <(user_prefix)
    "${pre[@]}" "$@"
}

load_env_cmd() {
    # Prints an `env -i ...` argv prefix for the session in $1.
    local dir="$1" f="$1/env"
    [[ -f "$f" ]] || die "no session env at $f (not started?)"
    local -a kv=()
    while IFS= read -r line; do [[ -n "$line" ]] && kv+=("$line"); done <"$f"
    printf '%s\0' env -i "${kv[@]}"
}

cmd_start() {
    local dir="${1:-}"
    if [[ -z "$dir" ]]; then
        dir="$(mktemp -d /tmp/pcpilot-headless.XXXXXX)"
    else
        mkdir -p "$dir"
    fi
    dir="$(cd "$dir" && pwd)"
    [[ -f "$dir/shell.pid" ]] && kill -0 "$(cat "$dir/shell.pid")" 2>/dev/null && die "already running in $dir"

    local home; home="$(user_home)"
    mkdir -p "$dir"/{runtime,data,config,cache,state}
    mkdir -p "$dir/data/gnome-shell/extensions" "$dir/config/glib-2.0/settings"
    rm -rf "$dir/data/gnome-shell/extensions/$UUID"
    if [[ -d "$REPO/extension/$UUID" ]]; then
        cp -r "$REPO/extension/$UUID" "$dir/data/gnome-shell/extensions/$UUID"
    fi
    local enabled="['$UUID']"
    [[ "${PCPILOT_HEADLESS_EXTENSION:-1}" == 0 ]] && enabled="@as []"
    cat >"$dir/config/glib-2.0/settings/keyfile" <<EOF
[org/gnome/shell]
enabled-extensions=$enabled
disable-user-extensions=false
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/input-sources]
sources=[('xkb', '$LAYOUT')]
xkb-options=@as []

[org/gnome/desktop/interface]
toolkit-accessibility=true
enable-animations=false

[org/gnome/desktop/a11y/applications]
screen-reader-enabled=false
EOF
    chown -R "$(uid_of):$(gid_of)" "$dir"
    chmod 0700 "$dir" "$dir/runtime"

    local sock="${PCPILOT_SOCKET:-$dir/pcpilot.sock}"
    # Environment for everything inside the session (the shell and `run` commands).
    # PCPILOT_HEADLESS_DIR doubles as the marker `stop` uses to find stray processes.
    cat >"$dir/env.base" <<EOF
HOME=$home
USER=$TEST_USER
LOGNAME=$TEST_USER
PATH=/usr/local/bin:/usr/bin:/bin
LANG=${LANG:-pt_BR.UTF-8}
XDG_RUNTIME_DIR=$dir/runtime
XDG_DATA_HOME=$dir/data
XDG_CONFIG_HOME=$dir/config
XDG_CACHE_HOME=$dir/cache
XDG_STATE_HOME=$dir/state
XDG_SESSION_TYPE=wayland
XDG_CURRENT_DESKTOP=GNOME
GSETTINGS_BACKEND=keyfile
NO_AT_BRIDGE=0
GTK_A11Y=atspi
WAYLAND_DISPLAY=wayland-pcpilot
PCPILOT_WAYLAND_SOCKET=$dir/runtime/wayland-pcpilot
PCPILOT_SOCKET=$sock
PCPILOT_HEADLESS_DIR=$dir
PCPILOT_TEST_HOOKS=${PCPILOT_TEST_HOOKS:-1}
EOF
    chown "$(uid_of):$(gid_of)" "$dir/env.base"

    # Inner launcher: record the private bus address, then exec gnome-shell.
    cat >"$dir/inner.sh" <<'EOF'
#!/bin/bash
set -e
d="$PCPILOT_HEADLESS_DIR"
{ cat "$d/env.base"; echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"; } >"$d/env.tmp"
mv "$d/env.tmp" "$d/env"
# The a11y bus (dbus-broker) activates the registry through systemd, which the private
# session lacks: start it by hand, inside this session so it lives and dies with it.
if [ "${PCPILOT_HEADLESS_ATSPI:-1}" != 0 ]; then
    /usr/lib/at-spi2-registryd >>"$d/atspi.log" 2>&1 </dev/null &
fi
exec gnome-shell --headless --wayland --no-x11 --wayland-display=wayland-pcpilot \
    --virtual-monitor "$PCPILOT_HEADLESS_SIZE"
EOF
    chmod 0755 "$dir/inner.sh"
    rm -f "$dir/env" "$dir/runtime/gnome-shell-disable-extensions"

    local -a envv=()
    while IFS= read -r line; do [[ -n "$line" ]] && envv+=("$line"); done <"$dir/env.base"
    envv+=("PCPILOT_HEADLESS_SIZE=$SIZE" "PCPILOT_HEADLESS_ATSPI=${PCPILOT_HEADLESS_ATSPI:-1}")
    [[ -n "${PCPILOT_EXTRA_ENV:-}" ]] && envv+=($PCPILOT_EXTRA_ENV)

    local -a pre=()
    while IFS= read -r -d '' x; do pre+=("$x"); done < <(user_prefix)
    cd /
    "${pre[@]}" setsid env -i "${envv[@]}" dbus-run-session -- "$dir/inner.sh" \
        >"$dir/shell.log" 2>&1 </dev/null &
    echo $! >"$dir/shell.pid"
    cd - >/dev/null

    # Ready = env written + wayland socket present + org.gnome.Shell owned on the bus.
    local i
    for i in $(seq 1 150); do
        if ! kill -0 "$(cat "$dir/shell.pid")" 2>/dev/null; then
            tail -n 30 "$dir/shell.log" >&2
            die "gnome-shell exited during startup (log: $dir/shell.log)"
        fi
        if [[ -f "$dir/env" && -S "$dir/runtime/wayland-pcpilot" ]]; then
            if cmd_run "$dir" -- gdbus call --session --dest org.freedesktop.DBus \
                --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner \
                org.gnome.Shell 2>/dev/null | grep -q true; then
                # The shell takes a moment after owning its name to load extensions.
                sleep 1
                echo "$dir"
                return 0
            fi
        fi
        sleep 0.2
    done
    die "timed out waiting for gnome-shell (log: $dir/shell.log)"
}

cmd_run() {
    local dir="$1"; shift
    [[ "${1:-}" == "--" ]] && shift
    [[ $# -gt 0 ]] || die "run: missing command"
    local -a pre=()
    while IFS= read -r -d '' x; do pre+=("$x"); done < <(load_env_cmd "$dir")
    # Stay in the caller's cwd when the test user can use it, else run from DIR.
    if as_user test -x . -a -r . 2>/dev/null; then
        as_user "${pre[@]}" "$@"
    else
        (cd "$dir" && as_user "${pre[@]}" "$@")
    fi
}

cmd_env() { cat "$1/env"; }

session_pids() {
    local dir="$1" p
    for p in /proc/[0-9]*; do
        { tr '\0' '\n' <"$p/environ"; } 2>/dev/null | grep -qxF "PCPILOT_HEADLESS_DIR=$dir" && echo "${p#/proc/}"
    done
    return 0
}

cmd_stop() {
    local dir; dir="$(cd "$1" && pwd)"
    local pid=""
    [[ -f "$dir/shell.pid" ]] && pid="$(cat "$dir/shell.pid")"
    if [[ -n "$pid" ]]; then kill -TERM -- "-$pid" 2>/dev/null || true; fi
    local i
    for i in $(seq 1 25); do
        [[ -z "$(session_pids "$dir")" ]] && break
        sleep 0.2
    done
    local left; left="$(session_pids "$dir")"
    [[ -n "$left" ]] && kill -KILL $left 2>/dev/null || true
    # FUSE mounts (gvfs, document portal) left in the private runtime dir.
    local m
    for m in $(awk -v d="$dir/" 'index($2, d) == 1 {print $2}' /proc/self/mounts); do
        fusermount3 -uz "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
    done
    rm -f "$dir/shell.pid"
}

cmd_status() {
    local dir="$1"
    if [[ -f "$dir/shell.pid" ]] && kill -0 "$(cat "$dir/shell.pid")" 2>/dev/null; then
        echo "running (pid $(cat "$dir/shell.pid"))"
    else
        echo "stopped"; return 1
    fi
}

case "${1:-}" in
    start) shift; cmd_start "$@" ;;
    run) shift; [[ $# -ge 1 ]] || die "usage: run DIR -- CMD"; cmd_run "$@" ;;
    env) shift; cmd_env "$1" ;;
    stop) shift; cmd_stop "$1" ;;
    status) shift; cmd_status "$1" ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac
