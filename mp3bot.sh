#!/usr/bin/env bash
# Day-to-day management. Installed as /usr/local/bin/mp3bot by install.sh.
set -Eeuo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
compose() { docker compose --project-directory "$DIR" -f "$DIR/docker-compose.yml" "$@"; }

usage() {
    cat <<'EOF'
Usage: mp3bot <command>

  status      is it running, and is the download server answering
  logs        follow the log (Ctrl+C to stop watching)
  restart     restart the bot
  stop        stop the bot
  start       start the bot
  login       log the Telegram account in again (or switch account)
  config      edit the settings (.env) and apply them
  update      download the latest version and restart
  uninstall   remove the bot (asks before deleting the data)
EOF
}

port() {
    local p
    p="$(awk -F= '$1 == "WEB_PORT" { print $2 }' "$DIR/.env" 2>/dev/null || true)"
    echo "${p:-8080}"
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
        git -C "$DIR" pull --ff-only
        chmod +x "$DIR/install.sh" "$DIR/mp3bot.sh"
        compose build -q bot
        compose up -d bot
        echo "updated to $(git -C "$DIR" rev-parse --short HEAD)"
        ;;
    uninstall)
        read -r -p "Remove the bot container and image? [y/N] " yes </dev/tty
        [ "$yes" = "y" ] || [ "$yes" = "Y" ] || exit 0
        compose down --rmi local || true
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
