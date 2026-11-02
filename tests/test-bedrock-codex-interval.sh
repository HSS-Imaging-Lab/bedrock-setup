#!/usr/bin/env bash
# Isolated integration tests: no real systemctl calls or credential refreshes.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d /tmp/codex-bedrock-interval-tests.XXXXXXXX)"
trap '[[ "$TEST_DIR" == /tmp/codex-bedrock-interval-tests.* ]] && rm -r -- "$TEST_DIR"' EXIT
TEST_BIN="$TEST_DIR/bin with spaces"
mkdir -p "$TEST_BIN"
for command_name in uname systemctl bedrock-codex; do
    ln -s "$REPO_DIR/tests/fixtures/interval-command" "$TEST_BIN/$command_name"
done
install -m 755 "$REPO_DIR/scripts/bedrock-codex-interval" "$TEST_BIN/bedrock-codex-interval"
export PATH="$TEST_BIN:$PATH"

begin_case() {
    export XDG_CONFIG_HOME="$TEST_DIR/$1/config"
    export TEST_SYSTEMCTL_LOG="$TEST_DIR/$1/systemctl.log"
    export TEST_TIMER_STATE=loaded TEST_SERVICE_STATE=not-found TEST_PLATFORM=Linux
    unset TEST_FAIL_ACTION
    mkdir -p "$XDG_CONFIG_HOME"
    OUTPUT="$TEST_DIR/$1/output"
    UNIT_DIR="$XDG_CONFIG_HOME/systemd/user"
}

expect_failure() {
    if bedrock-codex-interval "$@" > "$OUTPUT" 2>&1; then
        echo "Expected failure: $*" >&2
        exit 1
    fi
}

for invalid in '' 0 -1 1.5 abc 030 '30s' $'30\nOnCalendar=*'; do
    begin_case "invalid-${invalid//[^a-zA-Z0-9]/_}"
    expect_failure "$invalid"
    [[ ! -e "$TEST_SYSTEMCTL_LOG" && ! -e "$UNIT_DIR" ]]
done
begin_case missing-argument
expect_failure
begin_case extra-argument
expect_failure 30 one.timer extra
[[ ! -e "$TEST_SYSTEMCTL_LOG" ]]

begin_case help
bedrock-codex-interval --help > "$OUTPUT"
[[ ! -e "$TEST_SYSTEMCTL_LOG" ]]

begin_case create
export TEST_TIMER_STATE=not-found
bedrock-codex-interval 3000 > "$OUTPUT"
grep -Eq '^ExecStart=".*bin with spaces/bedrock-codex" --auth$' "$UNIT_DIR/codex-bedrock-refresh.service"
grep -Eq '^Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"$' "$UNIT_DIR/codex-bedrock-refresh.service"
grep -Eq '^WantedBy=timers.target$' "$UNIT_DIR/codex-bedrock-refresh.timer"
grep -Eq '^OnActiveSec=$' "$UNIT_DIR/codex-bedrock-refresh.timer.d/interval.conf"
grep -Eq '^OnActiveSec=3000s$' "$UNIT_DIR/codex-bedrock-refresh.timer.d/interval.conf"
grep -Eq '^OnUnitActiveSec=3000s$' "$UNIT_DIR/codex-bedrock-refresh.timer.d/interval.conf"
grep -Eq '^--user enable codex-bedrock-refresh.timer$' "$TEST_SYSTEMCTL_LOG"
grep -Eq '^--user restart codex-bedrock-refresh.timer$' "$TEST_SYSTEMCTL_LOG"
if command -v systemd-analyze >/dev/null 2>&1; then
    SYSTEMD_UNIT_PATH="$UNIT_DIR:" systemd-analyze --user verify \
        "$UNIT_DIR/codex-bedrock-refresh.service" "$UNIT_DIR/codex-bedrock-refresh.timer"
fi

# A later interval change must not rewrite an existing timer or service.
export TEST_TIMER_STATE=loaded
cp "$UNIT_DIR/codex-bedrock-refresh.service" "$TEST_DIR/original.service"
cp "$UNIT_DIR/codex-bedrock-refresh.timer" "$TEST_DIR/original.timer"
bedrock-codex-interval 30 > "$OUTPUT"
cmp "$TEST_DIR/original.service" "$UNIT_DIR/codex-bedrock-refresh.service"
cmp "$TEST_DIR/original.timer" "$UNIT_DIR/codex-bedrock-refresh.timer"
grep -Eq '^OnActiveSec=30s$' "$UNIT_DIR/codex-bedrock-refresh.timer.d/interval.conf"

begin_case existing-service
export TEST_TIMER_STATE=not-found TEST_SERVICE_STATE=loaded
mkdir -p "$UNIT_DIR"
printf '%s\n' 'existing service must be preserved' > "$UNIT_DIR/codex-bedrock-refresh.service"
bedrock-codex-interval 3000 > "$OUTPUT"
grep -Eq '^existing service must be preserved$' "$UNIT_DIR/codex-bedrock-refresh.service"

begin_case named-timer
bedrock-codex-interval 30 curatems-bedrock-auth-50m.timer > "$OUTPUT"
[[ ! -e "$UNIT_DIR/codex-bedrock-refresh.service" && ! -e "$UNIT_DIR/codex-bedrock-refresh.timer" ]]
grep -Eq '^OnUnitActiveSec=30s$' "$UNIT_DIR/curatems-bedrock-auth-50m.timer.d/interval.conf"
grep -Eq '^--user restart curatems-bedrock-auth-50m.timer$' "$TEST_SYSTEMCTL_LOG"
if grep -Eq ' enable | show .*\.service' "$TEST_SYSTEMCTL_LOG"; then
    echo 'Existing timers must not be enabled or have their services replaced.' >&2
    exit 1
fi

begin_case unknown-timer
export TEST_TIMER_STATE=not-found
expect_failure 3000 missing.timer
[[ ! -e "$UNIT_DIR" ]]

for timer_name in '../other.timer' '/etc/systemd/system/other.timer' other.service; do
    begin_case invalid-timer
    expect_failure 30 "$timer_name"
    [[ ! -e "$TEST_SYSTEMCTL_LOG" && ! -e "$UNIT_DIR" ]]
done

begin_case no-user-manager
export TEST_FAIL_ACTION=list-timers
expect_failure 3000
grep -Eq 'cannot connect to your systemd user manager' "$OUTPUT"
[[ ! -e "$UNIT_DIR" ]]

begin_case broken-timer
export TEST_TIMER_STATE=error
expect_failure 3000
[[ ! -e "$UNIT_DIR" ]]

begin_case reload-failure
export TEST_FAIL_ACTION=daemon-reload
expect_failure 30 existing.timer
if grep -Eq ' restart | enable ' "$TEST_SYSTEMCTL_LOG"; then
    echo 'Must not restart after a failed reload.' >&2
    exit 1
fi

begin_case macos
export TEST_PLATFORM=Darwin
expect_failure 30
grep -Eq 'use bedrock-codex-interval-macos.sh on macOS' "$OUTPUT"
[[ ! -e "$TEST_SYSTEMCTL_LOG" && ! -e "$UNIT_DIR" ]]

begin_case unsupported-platform
export TEST_PLATFORM=FreeBSD
expect_failure 30
grep -Eq 'this script requires Linux with systemd' "$OUTPUT"
[[ ! -e "$TEST_SYSTEMCTL_LOG" && ! -e "$UNIT_DIR" ]]

printf '%s\n' 'All interval script tests passed.'
