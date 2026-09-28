#!/usr/bin/env bash
# One-command installer for mp3-collector-bot.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/PoriyaVali/mp3-collector-bot/main/install.sh)
#
# Installs Docker if missing, downloads the newest release, asks for the settings,
# logs the Telegram account in, starts everything and turns on automatic updates.
# Safe to run again: it moves to the newest release and keeps your settings and data.
#
# Non-interactive use: export API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS
# (and optionally WEB_PORT, BASE_URL) before running; SKIP_LOGIN=1 skips the login step.
# INSTALL_REF=v1.2.3 installs that version; INSTALL_REF=keep uses the code already in INSTALL_DIR.
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/PoriyaVali/mp3-collector-bot.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/mp3-collector-bot}"
RECONFIGURE=0

for arg in "$@"; do
    case "$arg" in
        --reconfigure) RECONFIGURE=1 ;;
        --dir=*) INSTALL_DIR="${arg#--dir=}" ;;
        -h|--help)
            echo "Usage: install.sh [--reconfigure] [--dir=/opt/mp3-collector-bot]"
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

if [ -t 1 ]; then
    C_RED=$'\e[31m'; C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'; C_BLUE=$'\e[36m'; C_BOLD=$'\e[1m'; C_OFF=$'\e[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_BOLD$C_BLUE" "$*" "$C_OFF"; }
ok()   { printf '%s  OK%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s  !!%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
die()  { printf '%s  ERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# --------------------------------------------------------------------------
step "Checking the system"
[ "$(id -u)" -eq 0 ] || die "run as root (for example: sudo -i, then run the command again)"
[ "$(uname -s)" = "Linux" ] || die "this installer supports Linux servers only"
HAVE_TTY=0
if { : </dev/tty; } 2>/dev/null; then HAVE_TTY=1; fi

PKG=""
for candidate in apt-get dnf yum apk; do
    if command -v "$candidate" >/dev/null 2>&1; then PKG="$candidate"; break; fi
done
APT_UPDATED=0
pkg_install() {
    case "$PKG" in
        apt-get)
            if [ "$APT_UPDATED" -eq 0 ]; then DEBIAN_FRONTEND=noninteractive apt-get update -qq; APT_UPDATED=1; fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf) dnf install -y -q "$@" ;;
        yum) yum install -y -q "$@" ;;
        apk) apk add --no-cache "$@" ;;
        *) return 1 ;;
    esac
}
for tool in curl git; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        pkg_install "$tool" || die "could not install $tool; install it and run again"
    fi
done
# shellcheck source=/dev/null
ok "$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-Linux}") / $(uname -m)"

# --------------------------------------------------------------------------
step "Docker"
if command -v docker >/dev/null 2>&1; then
    ok "already installed: $(docker --version)"
else
    echo "  Docker is not installed; installing it now..."
    if curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh; then
        ok "installed with the official script"
    else
        warn "the official script failed; trying the distribution's package"
        pkg_install docker.io || pkg_install docker || die "could not install Docker"
    fi
    rm -f /tmp/get-docker.sh
fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker >/dev/null 2>&1 || true
elif command -v service >/dev/null 2>&1; then
    service docker start >/dev/null 2>&1 || true
fi
docker info >/dev/null 2>&1 || die "Docker is installed but not running (try: systemctl start docker)"

if ! docker compose version >/dev/null 2>&1; then
    echo "  Installing the Docker Compose plugin..."
    pkg_install docker-compose-plugin >/dev/null 2>&1 || pkg_install docker-compose-v2 >/dev/null 2>&1 || true
    if ! docker compose version >/dev/null 2>&1; then
        arch="$(uname -m)"
        mkdir -p /usr/local/lib/docker/cli-plugins
        curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
            "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" \
            || die "could not download Docker Compose"
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    fi
fi
ok "$(docker compose version)"

# --------------------------------------------------------------------------
step "Downloading the bot to $INSTALL_DIR"
# safe.directory: the checkout may belong to another user than root
git_in() { git -c safe.directory="$INSTALL_DIR" -c advice.detachedHead=false -C "$INSTALL_DIR" "$@"; }
if [ -d "$INSTALL_DIR/.git" ]; then
    git_in fetch -q --tags origin || warn "could not reach GitHub; keeping the current code"
else
    tmp_clone="$(mktemp -d)"
    git clone -q "$REPO_URL" "$tmp_clone/repo" || die "could not download $REPO_URL"
    mkdir -p "$INSTALL_DIR"
    # Copy in (instead of cloning in place) so leftovers of an earlier attempt, like .env or data/, survive.
    cp -a "$tmp_clone/repo/." "$INSTALL_DIR/"
    rm -rf "$tmp_clone"
fi
# INSTALL_REF: "latest" (default) = the newest release tag; "keep" = the code as it is; or a tag like v1.1.0
case "${INSTALL_REF:-latest}" in
    keep) ;;
    latest)
        tags="$(git_in tag -l 'v[0-9]*' --sort=-v:refname)"
        latest_tag=""
        read -r latest_tag <<< "$tags" || true
        if [ -n "$latest_tag" ]; then
            git_in checkout -q "$latest_tag" || die "could not switch to $latest_tag (files changed by hand in $INSTALL_DIR?)"
        else
            warn "no releases yet; using the main branch"
        fi
        ;;
    *) git_in checkout -q "$INSTALL_REF" || die "there is no version $INSTALL_REF" ;;
esac
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/mp3bot.sh"
cd "$INSTALL_DIR"
mkdir -p data
ok "version $(awk -F'"' '/^__version__/ { print $2 }' app/__init__.py) ($(git_in rev-parse --short HEAD))"

# --------------------------------------------------------------------------
ENV_FILE="$INSTALL_DIR/.env"
env_get() {
    if [ -f "$ENV_FILE" ]; then
        K="$1" awk -F= '$1 == ENVIRON["K"] { sub(/^[^=]*=/, ""); v = $0 } END { print v }' "$ENV_FILE"
    fi
}
env_set() {
    K="$1" V="$2" awk '
        BEGIN { done = 0 }
        index($0, ENVIRON["K"] "=") == 1 { print ENVIRON["K"] "=" ENVIRON["V"]; done = 1; next }
        { print }
        END { if (!done) print ENVIRON["K"] "=" ENVIRON["V"] }
    ' "$ENV_FILE" > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
}

is_int()   { [[ "$1" =~ ^[0-9]+$ ]]; }
is_hash()  { [[ "$1" =~ ^[0-9a-fA-F]{32}$ ]] || { echo "  API_HASH is 32 hex characters" >&2; return 1; }; }
is_token() { [[ "$1" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "  that does not look like a bot token (123456:ABC...)" >&2; return 1; }; }
is_ids()   { [[ "$1" =~ ^[0-9]+([,\ ]+[0-9]+)*$ ]] || { echo "  numeric ids only, comma separated" >&2; return 1; }; }
is_port() {
    if is_int "$1" && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; then return 0; fi
    echo "  a port is 1-65535" >&2
    return 1
}
is_url()   { [[ "$1" =~ ^https?://[^[:space:]]+$ ]] || { echo "  must start with http:// or https://" >&2; return 1; }; }

# ask VAR "question" "default" validator  (a value already in the environment wins)
ask() {
    local var="$1"
    local question="$2"
    local default="$3"
    local check="$4"
    local answer="${!var:-}"
    if [ -n "$answer" ] && "$check" "$answer"; then
        return 0
    fi
    [ "$HAVE_TTY" -eq 1 ] || die "$var is required (no terminal to ask on; export $var=...)"
    while true; do
        if [ -n "$default" ]; then
            printf '  %s [%s]: ' "$question" "$default" >/dev/tty
        else
            printf '  %s: ' "$question" >/dev/tty
        fi
        IFS= read -r answer </dev/tty || die "could not read from the terminal"
        answer="${answer:-$default}"
        answer="${answer#"${answer%%[![:space:]]*}"}"
        answer="${answer%"${answer##*[![:space:]]}"}"
        if [ -z "$answer" ]; then echo "  this one is required" >/dev/tty; continue; fi
        if "$check" "$answer" 2>/dev/tty; then break; fi
    done
    printf -v "$var" '%s' "$answer"
}

step "Settings"
if [ -f "$ENV_FILE" ] && [ "$RECONFIGURE" -eq 0 ] && [ -n "$(env_get BOT_TOKEN)" ]; then
    ok "keeping the existing settings in $ENV_FILE (run with --reconfigure to change them)"
else
    [ -f "$ENV_FILE" ] || cp .env.example "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    cat <<'EOF'
  You need:
   1. API_ID and API_HASH  -> https://my.telegram.org -> "API development tools"
   2. A bot token          -> @BotFather -> /newbot
   3. Your numeric id      -> send any message to @userinfobot
EOF
    ask API_ID    "API_ID"                         "$(env_get API_ID)"    is_int
    ask API_HASH  "API_HASH"                       "$(env_get API_HASH)"  is_hash
    ask BOT_TOKEN "Bot token"                      "$(env_get BOT_TOKEN)" is_token
    ask ADMIN_IDS "Admin numeric id(s), comma separated" "$(env_get ADMIN_IDS)" is_ids
    ask WEB_PORT  "Port for the download links"    "$(env_get WEB_PORT)"  is_port

    public_ip="$(curl -fsS4 --max-time 6 https://api.ipify.org 2>/dev/null || curl -fsS4 --max-time 6 https://ifconfig.me 2>/dev/null || true)"
    default_url="$(env_get BASE_URL)"
    case "$default_url" in ""|*YOUR_SERVER_IP*) default_url="http://${public_ip:-YOUR_SERVER_IP}:${WEB_PORT}" ;; esac
    ask BASE_URL "Public address of the download links" "$default_url" is_url

    env_set API_ID "$API_ID"
    env_set API_HASH "$API_HASH"
    env_set BOT_TOKEN "$BOT_TOKEN"
    env_set ADMIN_IDS "${ADMIN_IDS// /}"
    env_set WEB_PORT "$WEB_PORT"
    env_set BASE_URL "${BASE_URL%/}"
    ok "saved to $ENV_FILE"
fi

WEB_PORT="$(env_get WEB_PORT)"; WEB_PORT="${WEB_PORT:-8080}"
BOT_TOKEN="$(env_get BOT_TOKEN)"
BOT_NAME=""
if reply="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null)"; then
    if [[ "$reply" =~ \"username\":\"([^\"]+)\" ]]; then
        BOT_NAME="${BASH_REMATCH[1]}"
        ok "bot token works: @$BOT_NAME"
    fi
else
    warn "could not verify the bot token with Telegram (wrong token, or Telegram unreachable from this server)"
fi

# --------------------------------------------------------------------------
compose() { docker compose --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/docker-compose.yml" "$@"; }

step "Building the Docker image (first time takes a minute or two)"
compose build -q bot
ok "image built"

step "Telegram account login"
# Never open the account's session from two containers at once.
compose stop bot >/dev/null 2>&1 || true
if [ "${SKIP_LOGIN:-0}" = "1" ]; then
    warn "skipped (SKIP_LOGIN=1); run 'mp3bot login' later"
elif compose run --rm -T bot python -m app.login --check >/dev/null 2>&1; then
    ok "already logged in"
else
    [ "$HAVE_TTY" -eq 1 ] || die "the login needs a terminal; run 'mp3bot login' from an SSH session"
    cat <<'EOF'
  The bot needs a normal Telegram account to JOIN channels and download from them
  (bots cannot join channels by themselves). A spare account is recommended.
  Telegram will send a login code to that account.
EOF
    if compose run --rm bot python -m app.login </dev/tty; then
        ok "logged in"
    else
        warn "login did not finish; run 'mp3bot login' to try again"
    fi
fi

# --------------------------------------------------------------------------
step "Starting"
compose up -d --remove-orphans
ln -sf "$INSTALL_DIR/mp3bot.sh" /usr/local/bin/mp3bot
if scheduler="$(bash "$INSTALL_DIR/mp3bot.sh" install-scheduler)"; then
    ok "automatic updates on ($scheduler); turn off in the bot's settings or with: mp3bot autoupdate off"
else
    warn "could not set up automatic updates; run 'mp3bot update' now and then"
fi

# Success = the bot logged in to Telegram AND the download server answers AND the
# container is not in a crash/restart loop (the web server can answer between restarts).
if bash "$INSTALL_DIR/mp3bot.sh" verify; then
    ok "the bot is online and the download server answers on port $WEB_PORT"
else
    warn "the bot is not healthy. Last log lines:"
    compose logs --tail 30 bot || true
    die "fix the problem above, then run: mp3bot restart"
fi

BASE_URL="$(env_get BASE_URL)"
cat <<EOF

${C_GREEN}${C_BOLD}Installed.${C_OFF}
  Bot:            ${BOT_NAME:+@$BOT_NAME}
  Download links: ${BASE_URL}
  Files & data:   ${INSTALL_DIR}/data

  Next: open the bot in Telegram with the admin account and press /start,
        then send it a channel link.
  Make sure port ${WEB_PORT}/tcp is open in your server provider's firewall.

  New versions are installed automatically (checked every few hours); if one
  fails to start, the previous version is put back by itself.

  Manage it with:  mp3bot status | logs | restart | login | config | update | autoupdate on/off | uninstall
EOF
