#!/usr/bin/env bash
# Day-to-day management and automatic updates. Installed as /usr/local/bin/mp3bot by install.sh.
#
# Versions are git tags vX.Y.Z on GitHub. `auto-update` is run every few minutes by a
# systemd timer (or cron); it checks GitHub every AUTO_UPDATE_HOURS (default 6) or when
# the admin presses "update now" in the bot. An update is: check out the new tag, build
# the image, restart, and wait for the bot to log in to Telegram. If the build fails the
# running bot is never touched; if the new version does not come online, the previous
# image and code are put back automatically.
set -Eeuo pipefail

DIR="${MP3BOT_DIR:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)}"
DATA="$DIR/data"
STATUS_FILE="$DATA/update-status"
LOG_FILE="$DATA/update.log"
CONTAINER="mp3-collector-bot"
IMAGE="mp3-collector-bot"

# `git checkout` replaces this very file during an update, so updates run from a copy.
if [ -z "${MP3BOT_COPY:-}" ] && { [ "${1:-}" = "update" ] || [ "${1:-}" = "auto-update" ]; }; then
    copy="$(mktemp /tmp/mp3bot.XXXXXX)"
    cp "$DIR/mp3bot.sh" "$copy"
    MP3BOT_COPY="$copy" MP3BOT_DIR="$DIR" exec bash "$copy" "$@"
fi
if [ -n "${MP3BOT_COPY:-}" ]; then
    trap 'rm -f "$MP3BOT_COPY"' EXIT
fi

compose() { docker compose --project-directory "$DIR" -f "$DIR/docker-compose.yml" "$@"; }
# safe.directory: root may run this on a checkout owned by another user
g() { git -c safe.directory="$DIR" -c advice.detachedHead=false -C "$DIR" "$@"; }
now() { date +%s; }
is_number() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

usage() {
    cat <<'EOF'
Usage: mp3bot <command>

  status            is it running, which version, is the download server answering
  logs              follow the log (Ctrl+C to stop watching)
  restart | stop | start
  login             log the Telegram account in again (or switch account)
  config            edit the settings (.env) and apply them
  update            install the newest version now (rolls back by itself if it fails)
  autoupdate on     install new versions automatically (default)
  autoupdate off    only tell the admin in the bot that a new version exists
  version           installed and newest version
  uninstall         remove the bot (asks before deleting the data)
EOF
}

# key=value files (.env and the update status) -------------------------------------
kv_get() {  # kv_get FILE KEY
    if [ -f "$1" ]; then
        K="$2" awk -F= '$1 == ENVIRON["K"] { sub(/^[^=]*=/, ""); v = $0 } END { print v }' "$1"
    fi
}
status_get() { kv_get "$STATUS_FILE" "$1"; }
status_set() {  # status_set KEY VALUE [KEY VALUE ...]
    mkdir -p "$DATA"
    touch "$STATUS_FILE"
    while [ "$#" -ge 2 ]; do
        local key="$1"
        local value="$2"
        shift 2
        value="$(printf '%s' "$value" | tr -s '[:cntrl:]' ' ')"
        K="$key" V="$value" awk '
            BEGIN { done = 0 }
            index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next }
            { print }
            END { if (!done) print ENVIRON["K"] "=" ENVIRON["V"] }
        ' "$STATUS_FILE" > "$STATUS_FILE.tmp"
        mv "$STATUS_FILE.tmp" "$STATUS_FILE"
    done
}
record_event() {  # record_event RESULT FROM TO ERROR
    status_set event "$1" event_from "$2" event_to "$3" event_error "$4" event_at "$(now)" \
        last_check_result "$1" last_check_error "$4"
}

port() {
    local p
    p="$(kv_get "$DIR/.env" WEB_PORT)"
    echo "${p:-8080}"
}

# versions -------------------------------------------------------------------------
app_version() { awk -F'"' '/^__version__/ { print $2 }' "$DIR/app/__init__.py"; }

version_gt() {  # is $1 newer than $2?
    [ "$1" != "$2" ] || return 1
    local top
    top="$( { echo "$1"; echo "$2"; } | sort -V | tail -n 1)"
    [ "$top" = "$1" ]
}

latest_release() {  # prints the newest vX.Y.Z tag on GitHub without the v; empty if none
    local remote refs line tag best=""
    remote="$(g remote get-url origin)"
    refs="$(timeout 60 git ls-remote --tags --refs "$remote" 'v*')" || return 1
    while IFS= read -r line; do
        tag="${line##*refs/tags/v}"
        [[ "$tag" =~ ^[0-9]+[.][0-9]+[.][0-9]+$ ]] || continue
        if [ -z "$best" ] || version_gt "$tag" "$best"; then
            best="$tag"
        fi
    done <<< "$refs"
    echo "$best"
}

checkout_tag() {  # checkout_tag X.Y.Z
    g fetch -q origin "refs/tags/v$1:refs/tags/v$1" \
        && g checkout -q "v$1"
}

checkout_latest() {  # used by install.sh: switch the code to the newest release, if there is one
    local latest
    if ! latest="$(latest_release)"; then
        echo "could not reach GitHub; keeping the current code"
        return 0
    fi
    if [ -z "$latest" ]; then
        echo "no releases yet; using the main branch"
        return 0
    fi
    checkout_tag "$latest"
    echo "version $latest"
}

# is the bot really up? ----------------------------------------------------------------
verify() {  # 0 = logged in to Telegram, download server answers, not in a restart loop
    local started logs restarts state health
    for _ in $(seq 1 45); do
        started="$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || true)"
        logs="$(docker logs ${started:+--since "$started"} "$CONTAINER" 2>&1 || true)"
        case "$logs" in
            *"fatal error"*|*"configuration error"*) return 1 ;;
            *"is online"*)
                restarts="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)"
                sleep 5
                state="$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo "missing 0")"
                health="$(curl -fsS --max-time 5 "http://127.0.0.1:$(port)/health" 2>/dev/null || true)"
                [ "$state" = "running $restarts" ] && [ "$health" = "ok" ]
                return
                ;;
        esac
        sleep 2
    done
    return 1
}

# updating -------------------------------------------------------------------------
roll_back() {  # roll_back PREVIOUS_REF
    g checkout -q "$1" || true
    if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
        docker image tag "$IMAGE:previous" "$IMAGE:latest"
    else
        compose build -q bot || true
    fi
    compose up -d --force-recreate bot || true
}

update_to_latest() {  # update_to_latest auto|manual|check
    local mode="$1"
    local current latest prev_ref last_to last_at
    current="$(app_version)"
    status_set last_check_at "$(now)" current "$current"
    if ! latest="$(latest_release)"; then
        status_set last_check_result error last_check_error "could not reach GitHub"
        log "could not reach GitHub"
        return 1
    fi
    status_set latest "${latest:-$current}"
    if [ -z "$latest" ] || ! version_gt "$latest" "$current"; then
        status_set last_check_result up_to_date last_check_error ""
        log "up to date ($current)"
        return 0
    fi
    if [ "$mode" = "check" ]; then
        status_set last_check_result available last_check_error ""
        log "version $latest is available (automatic install is off)"
        return 0
    fi
    last_to="$(status_get event_to)"
    last_at="$(status_get event_at)"
    is_number "$last_at" || last_at=0
    if [ "$mode" = "auto" ] && [ "$last_to" = "$latest" ] && [ "$(status_get event)" != "updated" ] \
        && [ $(( $(now) - last_at )) -lt 86400 ]; then
        status_set last_check_result skipped last_check_error "v$latest failed less than a day ago"
        log "skipping $latest: it failed less than a day ago"
        return 0
    fi

    log "updating $current -> $latest"
    status_set last_check_result updating last_check_error ""
    prev_ref="$(g rev-parse HEAD)"
    if ! checkout_tag "$latest"; then
        g checkout -q "$prev_ref" || true
        record_event failed "$current" "$latest" "could not download v$latest (files changed by hand in $DIR?)"
        log "could not check out v$latest"
        return 1
    fi
    if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
        docker image tag "$IMAGE:latest" "$IMAGE:previous" || true
    fi
    if ! compose build -q bot; then
        # the running container still uses the old image: only the code goes back
        g checkout -q "$prev_ref"
        record_event failed "$current" "$latest" "building v$latest failed"
        log "build of $latest failed; nothing was changed"
        return 1
    fi
    if compose up -d bot && verify; then
        record_event updated "$current" "$latest" ""
        docker image rm "$IMAGE:previous" >/dev/null 2>&1 || true
        docker image prune -f >/dev/null 2>&1 || true
        log "updated to $latest"
        return 0
    fi
    log "$latest did not come online; rolling back to $current"
    docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/^/    /' || true
    roll_back "$prev_ref"
    record_event rolled_back "$current" "$latest" "v$latest did not come online"
    return 1
}

auto_update() {  # run by the timer every few minutes; usually does nothing
    local mode="auto" hours last
    if [ -f "$DATA/update-now" ]; then
        rm -f "$DATA/update-now"
        mode="manual"
    else
        hours="$(kv_get "$DIR/.env" AUTO_UPDATE_HOURS)"
        if ! is_number "$hours" || [ "$hours" -eq 0 ]; then
            hours=6
        fi
        last="$(status_get last_check_at)"
        is_number "$last" || last=0
        [ $(( $(now) - last )) -ge $(( hours * 3600 )) ] || return 0
        if [ -f "$DATA/auto-update-off" ]; then
            mode="check"
        fi
    fi
    update_to_latest "$mode"
}

run_locked() {  # one update at a time (timer and a manual run may meet)
    mkdir -p "$DATA"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$DATA/.update.lock"
        if ! flock -n 9; then
            log "another update is already running"
            return 0
        fi
    fi
    "$@"
}

trim_log() {
    if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 2000 ]; then
        tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

install_scheduler() {  # systemd timer when available, otherwise cron
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        cat > /etc/systemd/system/mp3bot-update.service <<EOF
[Unit]
Description=mp3-collector-bot: install a new version if there is one
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/mp3bot.sh auto-update
EOF
        cat > /etc/systemd/system/mp3bot-update.timer <<'EOF'
[Unit]
Description=mp3-collector-bot: look for a new version every few minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
        systemctl daemon-reload
        systemctl enable --now mp3bot-update.timer >/dev/null 2>&1
        rm -f /etc/cron.d/mp3bot-update
        echo "systemd timer"
    else
        echo "*/5 * * * * root $DIR/mp3bot.sh auto-update >/dev/null 2>&1" > /etc/cron.d/mp3bot-update
        chmod 644 /etc/cron.d/mp3bot-update
        echo "cron"
    fi
}

remove_scheduler() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now mp3bot-update.timer >/dev/null 2>&1 || true
        rm -f /etc/systemd/system/mp3bot-update.service /etc/systemd/system/mp3bot-update.timer
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f /etc/cron.d/mp3bot-update
}

show_version() {
    local latest
    echo "installed: $(app_version)"
    if latest="$(latest_release)"; then
        echo "newest:    ${latest:-(no releases yet)}"
    else
        echo "newest:    (could not reach GitHub)"
    fi
    if [ -f "$DATA/auto-update-off" ]; then
        echo "automatic updates: OFF (the admin is only told about new versions)"
    else
        echo "automatic updates: ON"
    fi
    if [ -f "$STATUS_FILE" ]; then
        echo "last check: $(date -d "@$(status_get last_check_at)" '+%Y-%m-%d %H:%M' 2>/dev/null || echo never) -> $(status_get last_check_result)"
    fi
}

cmd="${1:-}"
case "$cmd" in
    status)
        compose ps
        if [ "$(curl -fsS --max-time 3 "http://127.0.0.1:$(port)/health" 2>/dev/null || true)" = "ok" ]; then
            echo "download server: OK (port $(port))"
        else
            echo "download server: NOT answering on port $(port)"
        fi
        show_version
        echo "--- last log lines ---"
        compose logs --tail 8 bot || true
        ;;
    logs) compose logs -f --tail 200 bot ;;
    restart) compose restart bot ;;
    stop) compose stop bot ;;
    start) compose up -d bot ;;
    login)
        compose stop bot >/dev/null 2>&1 || true
        compose run --rm bot python -m app.login || echo "login did not finish"
        compose up -d bot
        ;;
    config)
        "${EDITOR:-nano}" "$DIR/.env"
        compose up -d --force-recreate bot
        ;;
    update)
        run_locked update_to_latest manual 2>&1 | tee -a "$LOG_FILE"
        ;;
    auto-update)
        mkdir -p "$DATA"
        trim_log
        run_locked auto_update >> "$LOG_FILE" 2>&1
        ;;
    autoupdate)
        mkdir -p "$DATA"
        case "${2:-}" in
            on) rm -f "$DATA/auto-update-off"; echo "automatic updates: ON" ;;
            off) touch "$DATA/auto-update-off"; echo "automatic updates: OFF (the admin will only be told about new versions)" ;;
            *) show_version ;;
        esac
        ;;
    version) show_version ;;
    verify) verify ;;
    checkout-latest) checkout_latest ;;
    install-scheduler) install_scheduler ;;
    uninstall)
        read -r -p "Remove the bot container and image? [y/N] " yes </dev/tty
        [ "$yes" = "y" ] || [ "$yes" = "Y" ] || exit 0
        remove_scheduler
        compose down --rmi all || true
        docker image rm "$IMAGE:previous" >/dev/null 2>&1 || true
        read -r -p "Also DELETE all data (login session, database, ZIP files) in $DIR? [y/N] " yes </dev/tty
        if [ "$yes" = "y" ] || [ "$yes" = "Y" ]; then
            rm -rf "$DIR"
            echo "deleted $DIR"
        else
            echo "data kept in $DIR"
        fi
        rm -f /usr/local/bin/mp3bot
        ;;
    ""|-h|--help|help) usage ;;
    *) echo "unknown command: $cmd"; usage; exit 1 ;;
esac
