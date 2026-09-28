#!/usr/bin/env bash
# CI only (needs root and Docker): install.sh + `mp3bot auto-update` end to end.
#
# A local bare repository stands in for GitHub, and in the fake releases the bot is
# replaced by a stub that either comes online or crashes. That way a successful update,
# an automatic rollback, a failed build and the on/off switch can all be produced for
# real, without Telegram credentials.
set -Eeuo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
REMOTE="$WORK/remote.git"
DEV="$WORK/dev"
DIR="$WORK/install"
STATUS="$DIR/data/update-status"

step() { echo; echo "=== $* ==="; }
pass() { echo "PASS: $*"; }
fail() {
    echo "FAIL: $*"
    echo "--- update-status ---"; cat "$STATUS" 2>/dev/null || true
    echo "--- update.log ---"; cat "$DIR/data/update.log" 2>/dev/null || true
    echo "--- container log ---"; docker logs --tail 30 mp3-collector-bot 2>&1 || true
    exit 1
}
status() { awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); v = $0 } END { print v }' "$STATUS"; }
installed() { awk -F'"' '/^__version__/ { print $2 }' "$DIR/app/__init__.py"; }
force_due() {  # pretend the last check was long ago
    mkdir -p "$DIR/data"
    touch "$STATUS"
    if grep -q '^last_check_at=' "$STATUS"; then
        sed -i 's/^last_check_at=.*/last_check_at=0/' "$STATUS"
    else
        echo "last_check_at=0" >> "$STATUS"
    fi
}
expect() {  # expect DESCRIPTION COMMAND...
    local what="$1"
    shift
    if "$@"; then pass "$what"; else fail "$what"; fi
}

release() {  # release VERSION ok|crash|broken
    local version="$1"
    local kind="$2"
    cp "$SRC/Dockerfile" "$DEV/Dockerfile"
    cat > "$DEV/app/__init__.py" <<EOF
"""CI build."""

__version__ = "$version"
REPO_URL = "https://example.invalid/ci"
EOF
    case "$kind" in
        ok) cp "$SRC/tests/stubs/online.py" "$DEV/app/main.py" ;;
        crash) cp "$SRC/tests/stubs/crash.py" "$DEV/app/main.py" ;;
        broken) echo "RUN false" >> "$DEV/Dockerfile" ;;
    esac
    git -C "$DEV" add -A
    git -C "$DEV" commit -q -m "release $version ($kind)"
    git -C "$DEV" tag "v$version"
    git -C "$DEV" push -q "$REMOTE" main "v$version"
    echo "released v$version ($kind)"
}

step "fake GitHub"
git init -q --bare -b main "$REMOTE"
git init -q -b main "$DEV"
git -C "$DEV" config user.email ci@example.invalid
git -C "$DEV" config user.name CI
git -c safe.directory="$SRC" -C "$SRC" archive HEAD | tar -x -C "$DEV"
release 1.0.0 ok

step "install.sh installs the newest release"
export API_ID=12345 API_HASH=0123456789abcdef0123456789abcdef \
    BOT_TOKEN=123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ADMIN_IDS=1 \
    WEB_PORT=8080 BASE_URL=http://127.0.0.1:8080 SKIP_LOGIN=1
if ! REPO_URL="$REMOTE" INSTALL_DIR="$DIR" bash "$SRC/install.sh" > "$WORK/install.log" 2>&1; then
    cat "$WORK/install.log"
    fail "install.sh"
fi
cat "$WORK/install.log"
expect "installer reports success" grep -q "^Installed\." "$WORK/install.log"
expect "installed version is 1.0.0" [ "$(installed)" = "1.0.0" ]
expect "update timer is enabled" systemctl is-enabled --quiet mp3bot-update.timer
# From here on the test drives auto-update itself: stop the timer and let a run it started finish.
systemctl disable --now mp3bot-update.timer
while systemctl is-active --quiet mp3bot-update.service; do sleep 1; done
MP3BOT="$DIR/mp3bot.sh"

step "a new release is installed automatically"
release 1.1.0 ok
force_due
"$MP3BOT" auto-update || true
expect "now on 1.1.0" [ "$(installed)" = "1.1.0" ]
expect "event is 'updated'" [ "$(status event)" = "updated" ]
expect "new container is the new version" "$MP3BOT" verify
expect "container log shows 1.1.0" bash -c "docker logs mp3-collector-bot 2>&1 | grep -q 'ci_stub_1.1.0 is online'"
expect "the previous image was cleaned up" bash -c "! docker image inspect mp3-collector-bot:previous >/dev/null 2>&1"

step "nothing happens before the check is due"
checked="$(status last_check_at)"
release 1.2.0 crash
"$MP3BOT" auto-update || true
expect "no new check" [ "$(status last_check_at)" = "$checked" ]
expect "still on 1.1.0" [ "$(installed)" = "1.1.0" ]

step "a release that crashes is rolled back ('update now' from the bot)"
touch "$DIR/data/update-now"
"$MP3BOT" auto-update || true
expect "request file consumed" [ ! -e "$DIR/data/update-now" ]
expect "back on 1.1.0" [ "$(installed)" = "1.1.0" ]
expect "event is 'rolled_back' to 1.2.0" [ "$(status event)/$(status event_to)" = "rolled_back/1.2.0" ]
expect "the old version is online again" "$MP3BOT" verify
expect "container log shows 1.1.0 again" bash -c "docker logs mp3-collector-bot 2>&1 | grep -q 'ci_stub_1.1.0 is online'"

step "the failed release is not retried automatically within a day"
force_due
"$MP3BOT" auto-update || true
expect "check result is 'skipped'" [ "$(status last_check_result)" = "skipped" ]
expect "still on 1.1.0" [ "$(installed)" = "1.1.0" ]

step "a release that does not build never touches the running bot"
release 1.3.0 broken
container="$(docker inspect -f '{{.Id}}' mp3-collector-bot)"
force_due
"$MP3BOT" auto-update || true
expect "event is 'failed' for 1.3.0" [ "$(status event)/$(status event_to)" = "failed/1.3.0" ]
expect "code is still 1.1.0" [ "$(installed)" = "1.1.0" ]
expect "same container kept running" [ "$(docker inspect -f '{{.Id}}' mp3-collector-bot)" = "$container" ]
expect "and it is healthy" "$MP3BOT" verify

step "automatic install switched off: only reported"
release 1.4.0 ok
"$MP3BOT" autoupdate off
force_due
"$MP3BOT" auto-update || true
expect "check result is 'available' with 1.4.0" [ "$(status last_check_result)/$(status latest)" = "available/1.4.0" ]
expect "not installed" [ "$(installed)" = "1.1.0" ]

step "'update now' still installs while automatic install is off"
touch "$DIR/data/update-now"
"$MP3BOT" auto-update || true
expect "now on 1.4.0" [ "$(installed)" = "1.4.0" ]
expect "event is 'updated'" [ "$(status event)/$(status event_to)" = "updated/1.4.0" ]
expect "online" "$MP3BOT" verify

step "manual update when already up to date"
"$MP3BOT" update > "$WORK/manual.log" 2>&1
cat "$WORK/manual.log"
expect "says up to date" grep -q "up to date (1.4.0)" "$WORK/manual.log"

step "re-running install.sh keeps settings and stays healthy"
if ! REPO_URL="$REMOTE" INSTALL_DIR="$DIR" bash "$SRC/install.sh" > "$WORK/reinstall.log" 2>&1; then
    cat "$WORK/reinstall.log"
    fail "second install.sh run"
fi
expect "reinstall kept the settings" grep -q "keeping the existing settings" "$WORK/reinstall.log"
expect "reinstall reports success" grep -q "^Installed\." "$WORK/reinstall.log"
expect "still on the newest release" [ "$(installed)" = "1.4.0" ]

echo
echo "ALL AUTO-UPDATE CHECKS PASSED"
