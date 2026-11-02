#!/usr/bin/env bash
# Isolated tests for scripts/bedrock-claude, scripts/bedrock-codex and
# scripts/codex-bedrock-creds. Every external command (uname, macaw-auth, aws,
# systemctl, launchctl, osascript, open, pgrep, pkill, codex, claude, bedrock-codex)
# is a fake from tests/fixtures/launcher-command, so nothing real is refreshed,
# installed or launched. The macOS path runs on any host, faked the same way.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d /tmp/bedrock-launcher-tests.XXXXXXXX)"
# A lock bug must not hang the suite: a watchdog ends it after five minutes. It polls in
# one-second steps so that it stops soon after the suite exits (a single long sleep would
# keep a pipe on stdout open until it ended).
( for _ in $(seq 300); do kill -0 $$ 2>/dev/null || exit 0; sleep 1; done
  echo 'FAIL: test suite timed out' >&2; kill -TERM $$ ) &
trap '[[ "$TEST_DIR" == /tmp/bedrock-launcher-tests.* ]] && rm -rf -- "$TEST_DIR"' EXIT
BASE_PATH="$PATH"
FAKES="uname date flock macaw-auth aws systemctl launchctl osascript open pgrep pkill codex claude"

# Fake commands plus copies of the launchers (so SELF is a known path with spaces).
make_bin() { # dir [command to leave out]
    mkdir -p "$1"
    local name
    for name in $FAKES; do
        [[ "$name" == "${2:-}" ]] || ln -s "$REPO_DIR/tests/fixtures/launcher-command" "$1/$name"
    done
    install -m 755 "$REPO_DIR/scripts/bedrock-codex" "$REPO_DIR/scripts/bedrock-claude" "$1/"
}
TEST_BIN="$TEST_DIR/bin with spaces"
make_bin "$TEST_BIN"
make_bin "$TEST_DIR/bin-no-launchctl" launchctl
export PATH="$TEST_BIN:$BASE_PATH"
UID_NOW="$(id -u)"

# ── Helpers ───────────────────────────────────────────────────
fail() {
    printf 'FAIL: %s\n' "$1" >&2
    [[ ! -f "${OUTPUT:-}" ]] || { echo '--- output ---' >&2; cat "$OUTPUT" >&2; }
    [[ ! -f "${TEST_LOG:-}" ]] || { echo '--- command log ---' >&2; cat "$TEST_LOG" >&2; }
    exit 1
}

# Fresh sandbox per case: fake HOME, XDG dirs, TMPDIR (with the trailing slash macOS has),
# credentials path, and none of the TEST_* knobs or launcher variables left over.
begin_case() {
    CASE="$TEST_DIR/$1"
    rm -rf -- "$CASE"
    export HOME="$CASE/home" XDG_CONFIG_HOME="$CASE/config" XDG_RUNTIME_DIR="$CASE/run"
    export TMPDIR="$CASE/tmp/"
    mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR" "$TMPDIR"
    export AWS_SHARED_CREDENTIALS_FILE="$HOME/.aws/credentials"
    export TEST_LOG="$CASE/calls.log"
    export TEST_PLATFORM=Linux TEST_TIMER_ENABLED=enabled
    unset TEST_FORBID_DASHDATE TEST_FORBID_FLOCK TEST_FRESH_CREDS TEST_MACAW_FAIL \
        TEST_NO_USER_MANAGER TEST_LAUNCHD_LOADED TEST_BEDROCK_FAIL RUN_STDIN \
        CODEX_BEDROCK_PROFILE CODEX_BEDROCK_REFRESH_THRESHOLD CODEX_BEDROCK_REFRESH_MIN \
        CODEX_BEDROCK_FROM_CREDS CLAUDE_BEDROCK_PROFILE CLAUDE_BEDROCK_REFRESH_THRESHOLD \
        AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION AWS_BEARER_TOKEN_BEDROCK OPENAI_API_KEY \
        AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN CLAUDE_CODE_USE_BEDROCK
    OUTPUT="$CASE/output"
    LOCK_BASE="${TMPDIR}"
    UNIT_DIR="$XDG_CONFIG_HOME/systemd/user"
    AGENT_DIR="$HOME/Library/LaunchAgents"
}

# write_creds FILE CODEX_SECS CLAUDE_SECS [utc|offset|z]: both profiles, each expiring
# that many seconds from now (negative = already expired), in the format macaw-auth writes.
write_creds() {
    python3 -I - "$@" <<'PY'
import os, sys
from datetime import datetime, timedelta, timezone
path, codex, claude = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
style = sys.argv[4] if len(sys.argv) > 4 else "utc"
def stamp(secs):
    when = datetime.now(timezone.utc) + timedelta(seconds=secs)
    if style == "offset":
        return when.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S+05:30")
    if style == "z":
        return when.strftime("%Y-%m-%d %H:%M:%SZ")
    return when.strftime("%Y-%m-%d %H:%M:%S+00:00")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as fh:
    for name, secs in (("codex_prod", codex), ("claude_prod", claude)):
        fh.write(f"[{name}]\naws_access_key_id = AKIAFAKE{name}\n"
                 f"aws_secret_access_key = secret{name}\naws_session_token = token{name}\n"
                 f"expiration = {stamp(secs)}\n\n")
PY
}

run() { # status in $STATUS, combined output in $OUTPUT; stdin from $RUN_STDIN
    if "$@" < "${RUN_STDIN:-/dev/null}" > "$OUTPUT" 2>&1; then STATUS=0; else STATUS=$?; fi
}
ok()  { run "$@"; [[ $STATUS -eq 0 ]] || fail "expected success (status $STATUS): $*"; }
bad() { run "$@"; [[ $STATUS -ne 0 ]] || fail "expected failure: $*"; }

out_has()   { grep -Eq -- "$1" "$OUTPUT" || fail "output lacks: $1"; }
file_has()  { grep -Eq -- "$2" "$1" 2>/dev/null || fail "$1 lacks: $2"; }
file_line() { grep -Fxq -- "$2" "$1" 2>/dev/null || fail "$1 lacks the exact line: $2"; }
log_has()   { file_has "$TEST_LOG" "$1"; }
log_exact() { file_line "$TEST_LOG" "$1"; }
log_lacks() {
    if [[ -f "$TEST_LOG" ]] && grep -Eq -- "$1" "$TEST_LOG"; then fail "log must not contain: $1"; fi
}
no_calls() { [[ ! -e "$TEST_LOG" ]] || fail "no command should have run"; }
log_before() { # two patterns; the first must appear on an earlier line than the second
    local a b
    a="$(grep -nEm1 -- "$1" "$TEST_LOG" | cut -d: -f1 || true)"
    b="$(grep -nEm1 -- "$2" "$TEST_LOG" | cut -d: -f1 || true)"
    [[ -n "$a" && -n "$b" && "$a" -lt "$b" ]] || fail "expected '$1' before '$2'"
}
nothing_written() { [[ -z "$(find "$HOME" "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR" "$TMPDIR" -mindepth 1 -print -quit)" ]] ||
    fail "files were written"; }
lock_dir() { printf '%s/%s-refresh-%s.lock.d' "${TMPDIR%/}" "$1" "$UID_NOW"; }

# ── Platform selection and --help ─────────────────────────────
for tool in bedrock-codex bedrock-claude; do
    for flags in '--mac --linux' '--linux --mac'; do
        begin_case "both-flags-$tool"
        # shellcheck disable=SC2086
        bad "$tool" $flags --auth
        out_has 'only one of --mac or --linux'
        no_calls
        nothing_written
    done

    begin_case "unknown-platform-$tool"
    export TEST_PLATFORM=FreeBSD
    bad "$tool" --auth
    out_has "unsupported platform 'FreeBSD'; pass --mac or --linux"
    no_calls
    nothing_written

    for flags in '--help' '-h' '--linux --help' '--mac -h'; do
        begin_case "help-$tool"
        export TEST_PLATFORM=FreeBSD
        # shellcheck disable=SC2086
        ok "$tool" $flags
        out_has '--mac'
        out_has '--linux'
        out_has '--auth'
        out_has '--install-timer'
        no_calls
        nothing_written
    done
done

# ── Linux: --auth ─────────────────────────────────────────────
begin_case linux-codex-valid
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60
ok bedrock-codex --linux --auth
out_has 'valid for [0-9]+s; refresh skipped'
out_has 'Authentication complete\.'
log_lacks '^macaw-auth'
log_lacks '^aws '
log_has '^date \[--date='
log_exact 'systemctl [--user] [is-enabled] [codex-bedrock-refresh.timer]'
log_lacks '\[enable\]'
file_line "$HOME/.codex/.env" 'AWS_PROFILE=codex_bedrock'
file_line "$HOME/.codex/.env" 'AWS_REGION=us-east-1'
[[ -n "$(find "$HOME/.codex/.env" -perm 600)" ]] || fail '.env must be mode 600'
[[ -e "$XDG_RUNTIME_DIR/bedrock-codex-refresh-$UID_NOW.lock" ]] || fail 'flock file missing'
[[ ! -e "$(lock_dir bedrock-codex)" ]]

begin_case linux-codex-expired
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
write_creds "$CASE/fresh" 3600 3600
export TEST_FRESH_CREDS="$CASE/fresh"
ok bedrock-codex --linux --auth
# Refresh uses the source profile, and verification must too: codex_bedrock's
# credential_process calls bedrock-codex, so verifying through it would recurse.
log_exact 'macaw-auth [login] [codex_prod] [--target-profile] [codex_prod]'
log_exact 'aws [sts] [get-caller-identity]'
log_exact 'aws.env AWS_PROFILE=codex_prod AWS_REGION=us-east-1'
log_lacks 'AWS_PROFILE=codex_bedrock'
log_lacks '\[--profile\]'
log_before '^macaw-auth' '^aws '
out_has 'AWS credentials are valid'

begin_case linux-codex-near-expiry
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 100 7200
ok bedrock-codex --linux --auth
log_has '^macaw-auth'

begin_case linux-codex-force
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
ok bedrock-codex --linux --auth --force
log_exact 'macaw-auth [login] [codex_prod] [--target-profile] [codex_prod]'
log_exact 'aws.env AWS_PROFILE=codex_prod AWS_REGION=us-east-1'

begin_case linux-codex-threshold-env
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 3600 7200
export CODEX_BEDROCK_REFRESH_THRESHOLD=7000
ok bedrock-codex --linux --auth
log_has '^macaw-auth'

begin_case linux-codex-from-creds
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
export CODEX_BEDROCK_FROM_CREDS=1 TEST_TIMER_ENABLED=disabled
ok bedrock-codex --linux --auth
log_has '^macaw-auth'
log_lacks '^systemctl'
[[ ! -e "$UNIT_DIR" ]]

begin_case linux-claude-valid
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
ok bedrock-claude --linux --auth
out_has 'refresh skipped'
out_has 'Authentication complete\.'
log_lacks '^macaw-auth'
log_lacks '^systemctl'
log_has '^date \[--date='
[[ -e "$XDG_RUNTIME_DIR/bedrock-claude-refresh-$UID_NOW.lock" ]] || fail 'flock file missing'

begin_case linux-claude-expired
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60
ok bedrock-claude --linux --auth
log_exact 'macaw-auth [login] [claude_prod] [--target-profile] [claude_prod]'
log_lacks '^aws '

begin_case linux-claude-force
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
ok bedrock-claude --linux --auth --force
log_exact 'macaw-auth [login] [claude_prod] [--target-profile] [claude_prod]'

# ── Missing prerequisites ─────────────────────────────────────
# A PATH holding only the fakes and the real tools the launchers use, minus one command.
REAL_TOOLS="bash env awk sed grep find mktemp mkdir rmdir id dirname basename cat chmod mv cp rm readlink sleep seq perl python3 tr cut ln head date flock"
isolated_path() { # omitted-command: sets PATH and keeps the old one in $SAVED_PATH
    local iso="$TEST_DIR/iso-$1" tool
    if [[ ! -d "$iso" ]]; then
        make_bin "$iso/bin" "$1"
        mkdir -p "$iso/real"
        for tool in $REAL_TOOLS; do # resolved on BASE_PATH, so a fake is never picked
            if [[ "$tool" != "$1" ]] && PATH="$BASE_PATH" command -v "$tool" >/dev/null 2>&1; then
                ln -s "$(PATH="$BASE_PATH" command -v "$tool")" "$iso/real/$tool"
            fi
        done
    fi
    SAVED_PATH="$PATH"
    PATH="$iso/bin:$iso/real"
}

begin_case missing-macaw-auth
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 -60
isolated_path macaw-auth
bad bedrock-codex --linux --auth
bad bedrock-claude --linux --auth
PATH="$SAVED_PATH"
out_has "'macaw-auth' not found on PATH"
log_lacks '^(macaw-auth|aws) '

begin_case missing-macaw-auth-but-valid
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
isolated_path macaw-auth
ok bedrock-codex --linux --auth
ok bedrock-claude --linux --auth
PATH="$SAVED_PATH"
out_has 'refresh skipped'

begin_case missing-aws
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
isolated_path aws
bad bedrock-codex --linux --auth
PATH="$SAVED_PATH"
out_has "'aws' not found on PATH"
log_lacks '^macaw-auth'

begin_case missing-codex
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
isolated_path codex
bad bedrock-codex --linux sol
PATH="$SAVED_PATH"
out_has "'codex' not found on PATH"
log_lacks '^macaw-auth'

begin_case missing-claude
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 -60
isolated_path claude
bad bedrock-claude --linux
PATH="$SAVED_PATH"
out_has "'claude' not found on PATH"
log_lacks '^macaw-auth'

begin_case missing-python3-on-mac
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 -60
isolated_path python3
bad bedrock-codex --mac --auth
out_has "'python3' not found on PATH"
bad bedrock-claude --mac --auth
PATH="$SAVED_PATH"
out_has "'python3' not found on PATH"
log_lacks '^macaw-auth'
[[ ! -e "$(lock_dir bedrock-codex)" && ! -e "$(lock_dir bedrock-claude)" ]]

# ── Linux: timer ──────────────────────────────────────────────
for tool in codex claude; do
    begin_case "linux-timer-$tool"
    export TEST_TIMER_ENABLED=disabled
    ok "bedrock-$tool" --linux --install-timer
    service="$UNIT_DIR/$tool-bedrock-refresh.service"
    timer="$UNIT_DIR/$tool-bedrock-refresh.timer"
    file_has "$service" "^ExecStart=\".*/bin with spaces/bedrock-$tool\" --linux --auth --force\$"
    file_has "$service" '^Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"$'
    file_has "$service" '^Type=oneshot$'
    file_has "$timer" "^Unit=$tool-bedrock-refresh.service\$"
    file_has "$timer" '^OnActiveSec=3000s$'
    file_has "$timer" '^OnUnitActiveSec=3000s$'
    file_has "$timer" '^WantedBy=timers.target$'
    log_exact 'systemctl [--user] [daemon-reload]'
    log_exact "systemctl [--user] [enable] [--now] [$tool-bedrock-refresh.timer]"
    log_lacks '^launchctl'
    if command -v systemd-analyze >/dev/null 2>&1; then
        SYSTEMD_UNIT_PATH="$UNIT_DIR:" systemd-analyze --user verify "$service" "$timer"
    fi

    begin_case "linux-timer-enabled-$tool"
    ok "bedrock-$tool" --linux --install-timer
    [[ ! -e "$UNIT_DIR" ]] || fail 'an enabled timer must not be rewritten'
    log_lacks '\[enable\]'

    begin_case "linux-timer-no-manager-$tool"
    export TEST_NO_USER_MANAGER=1 TEST_TIMER_ENABLED=disabled
    ok "bedrock-$tool" --linux --install-timer
    out_has 'No systemd user manager'
    [[ ! -e "$UNIT_DIR" ]]
done

# ── macOS: LaunchAgent ────────────────────────────────────────
check_plist() { # plist label exe logdir
    python3 -I - "$@" "$HOME" <<'PY'
import plistlib, sys
plist, label, exe, logdir, home = sys.argv[1:6]
with open(plist, "rb") as fh:
    d = plistlib.load(fh)
assert d["Label"] == label, d["Label"]
assert d["ProgramArguments"] == [exe, "--mac", "--auth", "--force"], d["ProgramArguments"]
assert d["StartInterval"] == 3000, d["StartInterval"]
assert d["RunAtLoad"] is False, d["RunAtLoad"]
assert d["EnvironmentVariables"]["PATH"] == f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
assert d["StandardOutPath"].startswith(logdir + "/"), d["StandardOutPath"]
assert d["StandardErrorPath"].startswith(logdir + "/"), d["StandardErrorPath"]
PY
}

for tool in codex claude; do
    label="com.hss.$tool-bedrock-refresh"
    begin_case "mac-timer-$tool"
    plist="$AGENT_DIR/$label.plist"
    ok "bedrock-$tool" --mac --install-timer
    check_plist "$plist" "$label" "$TEST_BIN/bedrock-$tool" "$HOME/.$tool/logs"
    [[ -d "$HOME/.$tool/logs" ]] || fail 'log directory missing'
    log_exact "launchctl [print] [gui/$UID_NOW/$label]"
    log_exact "launchctl [bootstrap] [gui/$UID_NOW] [$plist]"
    log_before '\[print\]' '\[bootstrap\]'
    log_lacks '^systemctl'
    [[ -z "$(find "$AGENT_DIR" -type f ! -name "$label.plist")" ]] || fail 'temp plist left behind'

    begin_case "mac-timer-loaded-$tool"
    export TEST_LAUNCHD_LOADED=1
    mkdir -p "$AGENT_DIR"
    printf '%s\n' sentinel > "$AGENT_DIR/$label.plist"
    ok "bedrock-$tool" --mac --install-timer
    file_line "$AGENT_DIR/$label.plist" sentinel
    log_exact "launchctl [print] [gui/$UID_NOW/$label]"
    log_lacks '\[bootstrap\]'

    begin_case "mac-timer-no-launchctl-$tool"
    PATH="$TEST_DIR/bin-no-launchctl:$BASE_PATH"
    ok "bedrock-$tool" --mac --install-timer
    PATH="$TEST_BIN:$BASE_PATH"
    out_has 'launchctl not found'
    [[ ! -e "$AGENT_DIR" ]]
done

# The plist is XML: &, < and > in the script path must be escaped, not break the file.
begin_case mac-timer-xml-escape
odd="$CASE/odd & <dir> \"q\""
mkdir -p "$odd"
install -m 755 "$REPO_DIR/scripts/bedrock-codex" "$odd/"
ok "$odd/bedrock-codex" --mac --install-timer
check_plist "$AGENT_DIR/com.hss.codex-bedrock-refresh.plist" com.hss.codex-bedrock-refresh \
    "$odd/bedrock-codex" "$HOME/.codex/logs"

# A symlinked install resolves to the real script for ProgramArguments.
begin_case mac-timer-symlink
mkdir -p "$CASE/links"
ln -s "$TEST_BIN/bedrock-claude" "$CASE/links/bedrock-claude"
ok "$CASE/links/bedrock-claude" --mac --install-timer
check_plist "$AGENT_DIR/com.hss.claude-bedrock-refresh.plist" com.hss.claude-bedrock-refresh \
    "$TEST_BIN/bedrock-claude" "$HOME/.claude/logs"

# ── macOS: --auth (python expiry, mkdir lock) ─────────────────
mac_case() { # name: macOS path with GNU-only tools forbidden and the agent "already loaded"
    begin_case "$1"
    export TEST_FORBID_DASHDATE=1 TEST_FORBID_FLOCK=1 TEST_LAUNCHD_LOADED=1
}

mac_case mac-codex-valid
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60
ok bedrock-codex --mac --auth
out_has 'valid for [0-9]+s; refresh skipped'
log_lacks '^macaw-auth'
log_lacks '^(date|flock) '
log_exact "launchctl [print] [gui/$UID_NOW/com.hss.codex-bedrock-refresh]"
log_lacks '^systemctl'
file_line "$HOME/.codex/.env" 'AWS_PROFILE=codex_bedrock'
[[ ! -e "$(lock_dir bedrock-codex)" ]] || fail 'lock directory not released'
[[ ! -e "$XDG_RUNTIME_DIR/bedrock-codex-refresh-$UID_NOW.lock" ]] || fail 'mac path must not use the flock file'

mac_case mac-codex-expired
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
ok bedrock-codex --mac --auth
log_exact 'macaw-auth [login] [codex_prod] [--target-profile] [codex_prod]'
log_exact 'aws.env AWS_PROFILE=codex_prod AWS_REGION=us-east-1'
log_lacks 'AWS_PROFILE=codex_bedrock'
log_lacks '^(date|flock) '
[[ ! -e "$(lock_dir bedrock-codex)" ]]

for style in offset z; do
    mac_case "mac-codex-timestamp-$style"
    write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60 "$style"
    ok bedrock-codex --mac --auth
    out_has 'refresh skipped'
    log_lacks '^macaw-auth'
    write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 -60 "$style"
    ok bedrock-codex --mac --auth
    log_has '^macaw-auth'
done

mac_case mac-codex-force
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
ok bedrock-codex --mac --auth --force
log_has '^macaw-auth'

mac_case mac-codex-from-creds
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
export CODEX_BEDROCK_FROM_CREDS=1
ok bedrock-codex --mac --auth
log_has '^macaw-auth'
log_lacks '^launchctl'

# A waiter that gets the lock after another process refreshed must not refresh again.
mac_case mac-codex-lock-wait
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
write_creds "$CASE/fresh" 3600 3600
mkdir "$(lock_dir bedrock-codex)"
(
    sleep 1
    cp "$CASE/fresh" "$AWS_SHARED_CREDENTIALS_FILE"
    rmdir "$(lock_dir bedrock-codex)"
) &
RELEASER=$!
ok bedrock-codex --mac --auth
wait "$RELEASER"
out_has 'refresh skipped'
log_lacks '^macaw-auth'
[[ ! -e "$(lock_dir bedrock-codex)" ]]

mac_case mac-codex-lock-stale
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
mkdir "$(lock_dir bedrock-codex)"
python3 -I -c 'import os, sys, time; t = time.time() - 600; os.utime(sys.argv[1], (t, t))' "$(lock_dir bedrock-codex)"
ok bedrock-codex --mac --auth
out_has 'Breaking stale lock'
[[ ! -e "$(lock_dir bedrock-codex)" ]]

mac_case mac-codex-lock-released-on-failure
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
export TEST_MACAW_FAIL=1
bad bedrock-codex --mac --auth
log_has '^macaw-auth'
[[ ! -e "$(lock_dir bedrock-codex)" ]] || fail 'lock directory left behind after a failed refresh'

mac_case mac-claude-valid
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
ok bedrock-claude --mac --auth
out_has 'refresh skipped'
log_lacks '^macaw-auth'
log_lacks '^(date|flock) '
log_lacks '^launchctl'
[[ ! -e "$(lock_dir bedrock-claude)" ]]

mac_case mac-claude-lock-released-on-failure
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60
export TEST_MACAW_FAIL=1
bad bedrock-claude --mac --auth
log_has '^macaw-auth'
[[ ! -e "$(lock_dir bedrock-claude)" ]] || fail 'lock directory left behind after a failed refresh'

mac_case mac-claude-expired
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 -60
ok bedrock-claude --mac --auth
log_exact 'macaw-auth [login] [claude_prod] [--target-profile] [claude_prod]'
log_lacks '^(date|flock) '
[[ ! -e "$(lock_dir bedrock-claude)" ]]

# No flag: Darwin from uname selects the mac path (no flock, no date --date).
mac_case mac-autodetect
export TEST_PLATFORM=Darwin
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
ok bedrock-codex --auth
ok bedrock-claude --auth
log_lacks '^(date|flock|systemctl) '
log_has '^launchctl'

# ── bedrock-codex --app ───────────────────────────────────────
write_config() {
    mkdir -p "$HOME/.codex"
    cat > "$HOME/.codex/config.toml" <<'EOF'
model = "global.openai.gpt-6-astra"
model_provider = "amazon-bedrock"
model_reasoning_effort = "high"

[model_providers.amazon-bedrock.aws]
profile = "codex_bedrock"
region = "us-east-1"
EOF
}

mac_case mac-app
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
write_config
ok bedrock-codex --mac --app sol
cfg="$HOME/.codex/config.toml"
file_line "$cfg" 'model = "global.openai.gpt-6-sol"'
file_line "$cfg" 'model_provider = "amazon-bedrock-runtime"'
file_line "$cfg" '[model_providers.amazon-bedrock-runtime.aws]'
file_line "$cfg" 'model_reasoning_effort = "high"'
file_line "$cfg" 'profile = "codex_bedrock"'
! grep -Fq 'amazon-bedrock.aws' "$cfg" || fail 'old provider table header survived'
log_exact 'osascript [-e] [quit app "ChatGPT"]'
log_exact 'pgrep [-f] [Resources/codex .*app-server]'
log_exact 'pkill [-f] [Resources/codex .*app-server]'
log_exact 'open [-a] [ChatGPT]'
log_before '^osascript' '^pkill'
log_before '^pkill' '^open'
out_has 'Codex Desktop relaunched'
# Running it again on an already-converted config changes nothing.
cp "$cfg" "$CASE/config.once"
ok bedrock-codex --mac --app sol
cmp "$cfg" "$CASE/config.once"

begin_case linux-app
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
write_config
ok bedrock-codex --linux --app sol
cfg="$HOME/.codex/config.toml"
file_line "$cfg" 'model = "global.openai.gpt-6-sol"'
file_line "$cfg" 'model_provider = "amazon-bedrock"'
file_line "$cfg" '[model_providers.amazon-bedrock.aws]'
file_line "$cfg" 'model_reasoning_effort = "high"'
log_lacks '^(osascript|open|pkill|pgrep) '
out_has 'Reload the Codex app session'

begin_case linux-app-missing-config
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
bad bedrock-codex --linux --app sol
out_has 'Missing Codex config'

begin_case mac-app-missing-config
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
bad bedrock-codex --mac --app sol
out_has 'Missing Codex config'
log_lacks '^(osascript|open|pkill) '

# ── bedrock-codex CLI launch ──────────────────────────────────
begin_case linux-codex-launch
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
export AWS_BEARER_TOKEN_BEDROCK=stale
ok bedrock-codex --linux sol
log_exact 'codex [--model] [global.openai.gpt-6-sol]'
log_exact 'codex.env AWS_PROFILE=codex_bedrock AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK= bearer=unset openai=unset akid=unset secret=unset token=unset'
out_has 'Launching Codex'

begin_case mac-codex-launch
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
export TEST_LAUNCHD_LOADED=1 TEST_FORBID_FLOCK=1 TEST_FORBID_DASHDATE=1
export AWS_BEARER_TOKEN_BEDROCK=stale OPENAI_API_KEY=stale AWS_ACCESS_KEY_ID=stale \
    AWS_SECRET_ACCESS_KEY=stale AWS_SESSION_TOKEN=stale
ok bedrock-codex --mac luna
log_exact 'codex [-c] [model_provider="amazon-bedrock-runtime"] [-c] [model_providers.amazon-bedrock-runtime.aws.profile="codex_bedrock"] [-c] [model_providers.amazon-bedrock-runtime.aws.region="us-east-1"] [--model] [global.openai.gpt-6-luna]'
log_exact 'codex.env AWS_PROFILE=codex_bedrock AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK= bearer=unset openai=unset akid=unset secret=unset token=unset'

begin_case autodetect-codex-launch
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
export TEST_LAUNCHD_LOADED=1
TEST_PLATFORM=Linux ok bedrock-codex astra
log_exact 'codex [--model] [global.openai.gpt-6-astra]'
rm -f "$TEST_LOG"
TEST_PLATFORM=Darwin ok bedrock-codex astra
log_has '^codex \[-c\] \[model_provider='

begin_case codex-menu
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
printf '3\n' > "$CASE/stdin"
RUN_STDIN="$CASE/stdin" ok bedrock-codex --linux
log_exact 'codex [--model] [global.openai.gpt-6-sol]'
out_has '3\) global.openai.gpt-6-sol'

begin_case codex-unknown-model
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
bad bedrock-codex --linux bogus
out_has 'Unknown model: bogus'
log_lacks '^codex'

# ── bedrock-claude launch ─────────────────────────────────────
for platform in linux mac; do
    begin_case "claude-launch-$platform"
    write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
    export TEST_LAUNCHD_LOADED=1
    ok bedrock-claude "--$platform" --resume abc -p 'hello world'
    log_exact 'claude [--resume] [abc] [-p] [hello world]'
    log_exact 'claude.env AWS_PROFILE=claude_prod AWS_REGION= AWS_DEFAULT_REGION= CLAUDE_CODE_USE_BEDROCK=1 bearer=unset openai=unset akid=unset secret=unset token=unset'
    log_lacks '\[--(mac|linux)\]'

    begin_case "claude-launch-noargs-$platform"
    write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
    export TEST_LAUNCHD_LOADED=1
    ok bedrock-claude "--$platform"
    log_exact 'claude'
done

begin_case claude-launch-autodetect
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 7200 7200
export TEST_LAUNCHD_LOADED=1 TEST_FORBID_FLOCK=1 TEST_FORBID_DASHDATE=1 TEST_PLATFORM=Darwin
ok bedrock-claude --model sonnet
log_exact 'claude [--model] [sonnet]'
log_lacks '^(date|flock) '

# ── codex-bedrock-creds ───────────────────────────────────────
# credential_process mode. PATH is pinned so a real bedrock-codex on the host is never found.
CREDS="$REPO_DIR/scripts/codex-bedrock-creds"
CREDS_PATH="$TEST_DIR/empty-bin:$(dirname "$(command -v python3)"):/usr/bin:/bin"
mkdir -p "$TEST_DIR/empty-bin"
run_creds() { # args...; stdout in $CASE/stdout, stderr in $CASE/stderr
    if PATH="$CREDS_PATH" "$CREDS" "$@" < /dev/null > "$CASE/stdout" 2> "$CASE/stderr"; then STATUS=0; else STATUS=$?; fi
    OUTPUT="$CASE/stderr"
}
fake_launcher() { # dir: install the fake bedrock-codex there
    mkdir -p "$1"
    ln -s "$REPO_DIR/tests/fixtures/launcher-command" "$1/bedrock-codex"
}
check_creds_json() { # stdout must be exactly one JSON document holding the codex_prod keys
    python3 -I - "$CASE/stdout" <<'PY'
import json, re, sys
raw = open(sys.argv[1]).read()
d = json.loads(raw)
assert raw.strip().startswith("{") and raw.strip().endswith("}"), raw
assert d["Version"] == 1
assert d["AccessKeyId"] == "AKIAFAKEcodex_prod", d
assert d["SecretAccessKey"] == "secretcodex_prod"
assert d["SessionToken"] == "tokencodex_prod"
assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", d["Expiration"]), d
PY
}

begin_case creds-expired
export AWS_SHARED_CREDENTIALS_FILE="$CASE/custom/credentials"
export AWS_BEARER_TOKEN_BEDROCK=stale
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
write_creds "$CASE/fresh" 3600 3600
export TEST_FRESH_CREDS="$CASE/fresh"
fake_launcher "$HOME/.local/bin"
run_creds
[[ $STATUS -eq 0 ]] || fail "creds script exited $STATUS"
check_creds_json
log_exact 'bedrock-codex [--auth]'
log_has "^bedrock-codex\\.env FROM_CREDS=1 THRESHOLD=360 bearer=unset path=$HOME/\\.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
log_lacks '^macaw-auth'
log_lacks '\[--(mac|linux)\]'
file_has "$HOME/.codex/logs/bedrock-refresh.log" 'refreshing via bedrock-codex --auth'
file_has "$HOME/.codex/logs/bedrock-refresh.log" 'bedrock-codex --auth exit=0: .*noise on stderr'

begin_case creds-fresh
unset AWS_SHARED_CREDENTIALS_FILE
write_creds "$HOME/.aws/credentials" 7200 -60
fake_launcher "$HOME/.local/bin"
run_creds
[[ $STATUS -eq 0 ]] || fail "creds script exited $STATUS"
check_creds_json
no_calls

begin_case creds-near-expiry
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 200 7200
write_creds "$CASE/fresh" 3600 3600
export TEST_FRESH_CREDS="$CASE/fresh"
fake_launcher "$HOME/.local/bin"
run_creds
[[ $STATUS -eq 0 ]] || fail "creds script exited $STATUS"
check_creds_json
log_has 'THRESHOLD=360 '

begin_case creds-scheduled-refresh
write_creds "$AWS_SHARED_CREDENTIALS_FILE" 1800 7200
fake_launcher "$HOME/.local/bin"
run_creds --refresh --min 10
[[ $STATUS -eq 0 ]] || fail "creds --refresh exited $STATUS"
no_calls
run_creds --refresh
[[ $STATUS -eq 0 ]] || fail "creds --refresh exited $STATUS"
[[ ! -s "$CASE/stdout" ]] || fail '--refresh must print nothing'
log_exact 'bedrock-codex [--auth]'
log_has 'FROM_CREDS=1 THRESHOLD=3300 '

begin_case creds-refresh-fails
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
export TEST_BEDROCK_FAIL=1
fake_launcher "$HOME/.local/bin"
run_creds
[[ $STATUS -eq 1 ]] || fail "expected exit 1, got $STATUS"
[[ ! -s "$CASE/stdout" ]] || fail 'stdout must stay empty when the refresh fails'
out_has 'refresh failed; run `bedrock-codex --auth`'
log_has '^bedrock-codex'
file_has "$HOME/.codex/logs/bedrock-refresh.log" 'bedrock-codex --auth exit=1'

begin_case creds-path-fallback
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
write_creds "$CASE/fresh" 3600 3600
export TEST_FRESH_CREDS="$CASE/fresh"
fake_launcher "$CASE/elsewhere"
CREDS_PATH="$CASE/elsewhere:$CREDS_PATH"
run_creds
CREDS_PATH="${CREDS_PATH#"$CASE/elsewhere:"}"
[[ $STATUS -eq 0 ]] || fail "creds script exited $STATUS"
check_creds_json
log_exact 'bedrock-codex [--auth]'

begin_case creds-no-launcher
write_creds "$AWS_SHARED_CREDENTIALS_FILE" -60 7200
run_creds
[[ $STATUS -eq 1 ]] || fail "expected exit 1, got $STATUS"
[[ ! -s "$CASE/stdout" ]] || fail 'stdout must stay empty'
file_has "$HOME/.codex/logs/bedrock-refresh.log" 'bedrock-codex not found'

printf '%s\n' 'All launcher tests passed.'
