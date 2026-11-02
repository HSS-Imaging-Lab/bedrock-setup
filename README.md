# Bedrock Setup

Run Claude Code and Codex on Amazon Bedrock, on macOS and Linux. The scripts keep
short-lived AWS credentials fresh so a session never hits an expired token. Copy
them into your own machine and add the config snippets below; nothing here is
installed for you.

## How the pieces connect

```text
bedrock-claude / bedrock-codex  --(refresh if needed)-->  macaw-auth  -->  ~/.aws/credentials
        |                                                                   [claude_prod] [codex_prod]
        +-- installs a timer: systemd user timer (Linux) / LaunchAgent (macOS)
            every 50 minutes it runs   <launcher> --auth --force

Claude Code  settings.json "awsAuthRefresh"  -->  bedrock-claude --auth
Codex        profile codex_bedrock           -->  credential_process: codex-bedrock-creds
                                                  -->  bedrock-codex --auth
```

| File | What it does |
| --- | --- |
| `scripts/bedrock-claude` | Launcher for Claude Code. Refreshes `claude_prod` if needed, installs the timer, then runs `claude` with `CLAUDE_CODE_USE_BEDROCK=1` and `AWS_PROFILE=claude_prod`. |
| `scripts/bedrock-codex` | Launcher for Codex. Refreshes `codex_prod`, verifies it, writes `~/.codex/.env`, installs the timer, then runs `codex` on the chosen model. `--app` sets the Codex app default model. |
| `scripts/codex-bedrock-creds` | The `credential_process` for the `codex_bedrock` profile. Prints the `codex_prod` keys as JSON and, when they are near expiry, runs `bedrock-codex --auth` first. |
| `scripts/bedrock-codex-interval` | Linux: change the refresh interval of the systemd user timer. |
| `scripts/bedrock-codex-interval-macos.sh` | macOS: change the refresh interval of the Codex LaunchAgent. |
| `templates/global-gpt6-models.json` | Codex model catalog for the GPT-6 Global models. |

Credentials are always checked and verified through the source profiles
(`claude_prod`, `codex_prod`), never through `codex_bedrock`: its
`credential_process` calls `bedrock-codex`, so verifying through it would recurse.

## Prerequisites

- `macaw-auth`, set up so `macaw-auth login <profile> --target-profile <profile>` works
  without prompting (password in the keyring). It writes the keys and an
  `expiration = ...` line to `~/.aws/credentials`.
- AWS CLI (`aws`).
- Claude Code (`claude`) and/or Codex (`codex`) on `PATH`.
- `perl` (used by `bedrock-codex --app`), and `python3` (the macOS launchers read the credential
  expiry with it; `codex-bedrock-creds` is Python 3 on both platforms).
- Linux: systemd with a user manager (`systemctl --user`) and `flock` (util-linux).
- macOS: `launchctl` (always present); `bash` 3.2 from the system is fine.

A script that needs something missing stops with a message naming it.

## Install

### Linux

```bash
mkdir -p ~/.local/bin ~/.codex
install -m 755 scripts/bedrock-claude scripts/bedrock-codex scripts/codex-bedrock-creds ~/.local/bin/
install -m 755 scripts/bedrock-codex-interval ~/.local/bin/    # optional
cp templates/global-gpt6-models.json ~/.codex/global-gpt6-models.json
loginctl enable-linger "$USER"    # keep the refresh timer running after you log out
```

### macOS

```bash
mkdir -p ~/.local/bin ~/.codex/scripts
install -m 755 scripts/bedrock-claude scripts/bedrock-codex scripts/codex-bedrock-creds ~/.local/bin/
install -m 755 scripts/bedrock-codex-interval-macos.sh ~/.codex/scripts/    # optional
cp templates/global-gpt6-models.json ~/.codex/global-gpt6-models.json
```

### Both

Make sure `~/.local/bin` is on `PATH` (for example `export PATH="$HOME/.local/bin:$PATH"` in
`~/.zshrc` or `~/.bashrc`). Then add the config below and run each launcher once to log in
and install its timer: `bedrock-codex --auth`, and `bedrock-claude --auth` followed by
`bedrock-claude --install-timer` (a normal launch of either tool also installs the timer;
`bedrock-claude --auth` does not, because Claude Code's `awsAuthRefresh` calls it).

## Configuration you add yourself

### `~/.aws/config`

Replace the `<...>` values with your own. `credential_process` needs an absolute path:
AWS does not expand `~` or `$HOME` there (on macOS it is `/Users/<you>/...`, on Linux `/home/<you>/...`).

```ini
# macaw-auth settings: copy these from your existing macaw-auth setup.
[macaw-auth]
session_duration = 3600
identity_url = <your identity provider sign-in URL>
username = <your username>
idp_name = <your IdP name>
enable_keyring = True
region = us-east-1
partition = aws

# Source profiles: macaw-auth writes short-lived keys into ~/.aws/credentials under these names.
[profile claude_prod]
role_name = <role>
account_number = <aws-account-id>

[profile codex_prod]
role_name = <role>
account_number = <aws-account-id>

# The profile Codex uses. Its keys come from codex_prod and refresh themselves.
[profile codex_bedrock]
credential_process = <absolute path to home>/.local/bin/codex-bedrock-creds
region = us-east-1
```

### `~/.claude/settings.json`

Merge these keys into the file; keep your other settings. `CLAUDE_CODE_SKIP_AWS_CRED_CACHE`
makes Claude Code re-read the credentials on every request, and `awsAuthRefresh` runs
if it still finds them expired.

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_PROFILE": "claude_prod",
    "AWS_REGION": "us-east-1",
    "CLAUDE_CODE_SKIP_AWS_CRED_CACHE": "1"
  },
  "awsAuthRefresh": "$HOME/.local/bin/bedrock-claude --auth"
}
```

If your Claude Code does not expand `$HOME` there, write the absolute path. To pin the
Bedrock models, also set `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`
and `ANTHROPIC_DEFAULT_HAIKU_MODEL` under `env` to the Bedrock model IDs enabled for your account.

### `~/.codex/config.toml`

Codex needs the Bedrock Runtime provider, the `codex_bedrock` AWS profile, and the model
catalog. TOML does not expand `$HOME`: replace `$HOME` below with your real home path.
Keep unrelated settings already in your file.

```toml
model = "global.openai.gpt-6-luna"
model_provider = "amazon-bedrock-runtime"
model_catalog_json = "$HOME/.codex/global-gpt6-models.json"
model_reasoning_effort = "xhigh"

[model_providers.amazon-bedrock-runtime]
wire_api = "responses"

[model_providers.amazon-bedrock-runtime.aws]
profile = "codex_bedrock"
region = "us-east-1"
```

macOS excerpt (the `notify` line is the optional Computer Use hook; omit it if you do not use that):

```toml
model = "global.openai.gpt-6-luna"
model_provider = "amazon-bedrock-runtime"
model_catalog_json = "$HOME/.codex/global-gpt6-models.json"
notify = ["$HOME/.codex/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient", "turn-ended"]
model_reasoning_effort = "xhigh"

[model_providers.amazon-bedrock-runtime.aws]
profile = "codex_bedrock"
```

Linux or remote-host excerpt (extra settings are examples; use what suits you):

```toml
model_provider = "amazon-bedrock-runtime"
model = "global.openai.gpt-6-luna"
model_catalog_json = "$HOME/.codex/global-gpt6-models.json"
model_reasoning_effort = "max"
sandbox_mode = "danger-full-access"
approval_policy = "on-request"

[model_providers.amazon-bedrock-runtime]
wire_api = "responses"

[model_providers.amazon-bedrock-runtime.aws]
profile = "codex_bedrock"
region = "us-east-1"
```

### GPT-6 Global models

The catalog (`templates/global-gpt6-models.json`) holds these model IDs:

- `global.openai.gpt-6-astra` - `6 Astra (Global)`
- `global.openai.gpt-6.1-sol` - `6.1 Sol (Global)`
- `global.openai.gpt-6-sol` - `6 Sol (Global)`
- `global.openai.gpt-6-luna` - `6 Luna (Global)`

Copy it to each host's `~/.codex/global-gpt6-models.json` and point `model_catalog_json`
at that host's absolute path. Keep an independent copy per host; do not symlink between
hosts. Keep the region at `us-east-1` for these Global models.

If CLI model selection works but a remote host's app picker is stale, open Codex Settings
-> Connections, select that connection, choose **Update**, then reconnect the session.
(`codex app-server daemon update --from-cli` was not the working path here.)

## Usage

Both launchers take an optional leading `--mac` or `--linux`. Without one, the platform is
detected from `uname -s` (Darwin is macOS, Linux is Linux); on anything else you must pass
one. Passing both is an error. `-h` / `--help` lists every mode.

| Command | What it does |
| --- | --- |
| `bedrock-claude [--mac\|--linux] [claude args...]` | Refresh if needed, install the timer, launch Claude Code. Everything after the platform flag goes to `claude`. |
| `bedrock-claude [--mac\|--linux] --auth [--force]` | Refresh only. `--force` skips the expiry check. This is what the timer and `awsAuthRefresh` run. |
| `bedrock-claude [--mac\|--linux] --install-timer` | Install and start the 50-minute refresh timer. |
| `bedrock-codex [--mac\|--linux] [astra\|sol\|luna]` | Refresh if needed, install the timer, launch the Codex CLI on that model (menu if omitted). |
| `bedrock-codex [--mac\|--linux] --auth [--force]` | Refresh, verify, write `~/.codex/.env`, install the timer. `-auth` also works. |
| `bedrock-codex [--mac\|--linux] --app [astra\|sol\|luna]` | Set the Codex app default model. macOS: also sets `model_provider` and the provider's `.aws` table name, quits ChatGPT, and reopens it. Linux: edits only `model =` in `~/.codex/config.toml`; reload the app session afterwards. |
| `bedrock-codex [--mac\|--linux] --install-timer` | Install and start the 50-minute refresh timer. |

Examples: `bedrock-codex --mac --app sol`, `bedrock-claude --linux --auth`,
`bedrock-claude --resume`.

Platform differences, for reference: Linux uses GNU `date`, `flock` (lock file in
`$XDG_RUNTIME_DIR`, else `/tmp`) and a systemd user timer. macOS parses the expiry with
`python3`, locks with an atomic `mkdir` directory in `$TMPDIR` (a lock older than 5 minutes
is treated as stale and broken), and uses a LaunchAgent. On macOS the Codex CLI is started
with `-c model_provider=... -c ...aws.profile=... -c ...aws.region=...`; on Linux it relies on
`config.toml`.

## The refresh timer

`--install-timer` (also run by every normal launch, and by `bedrock-codex --auth`) sets up a
timer that runs `<launcher> --<platform> --auth --force` every 3000 seconds (50 minutes), so the
roughly one-hour STS token never lapses. It does nothing if the timer is already installed.
The timer's own `--auth` call is passed the platform flag explicitly. When `codex-bedrock-creds`
runs `bedrock-codex --auth` it sets `CODEX_BEDROCK_FROM_CREDS=1`, which skips the timer install.

| | Linux | macOS |
| --- | --- | --- |
| Unit | `claude-bedrock-refresh.{service,timer}` and `codex-bedrock-refresh.{service,timer}` in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/` | `~/Library/LaunchAgents/com.hss.claude-bedrock-refresh.plist` and `com.hss.codex-bedrock-refresh.plist` (`StartInterval` 3000, `RunAtLoad` false) |
| Logs | `journalctl --user -u codex-bedrock-refresh.service` | `~/.codex/logs/bedrock-refresh-launchd.{out,err}.log` and `~/.claude/logs/...` |
| Inspect | `systemctl --user list-timers 'codex-bedrock-refresh.timer' --all` | `launchctl print gui/$(id -u)/com.hss.codex-bedrock-refresh` |
| Stop | `systemctl --user disable --now codex-bedrock-refresh.timer` | `launchctl bootout gui/$(id -u)/com.hss.codex-bedrock-refresh` |
| Remove | delete the two unit files, `systemctl --user daemon-reload` | `rm ~/Library/LaunchAgents/com.hss.codex-bedrock-refresh.plist` |

For the Claude timer use the `claude-bedrock-refresh` names. Stopping a timer is temporary: the
next launcher run installs it again, so delete the unit files or plist to remove it for good. The
scripts leave an installed timer alone, so to change its interval use the interval helpers below.

### macOS refresh interval

```text
scripts/bedrock-codex-interval-macos.sh
```

Changes the macOS Codex LaunchAgent refresh interval. The agent must already exist, so
run `bedrock-codex --install-timer` first:

```bash
bash ~/.codex/scripts/bedrock-codex-interval-macos.sh 3000
```

`3000` seconds is 50 minutes. This helper does not work on Linux and only edits the Codex
agent; for the Claude agent, edit `StartInterval` in its plist and reload it.

### Linux refresh interval

```text
scripts/bedrock-codex-interval
```

Changes the Linux per-user refresh interval. This separate script is standalone and can be
installed in `~/.local/bin` alongside `bedrock-codex`:

```bash
mkdir -p ~/.local/bin
install -m 755 scripts/bedrock-codex-interval ~/.local/bin/bedrock-codex-interval
```

Run the executable directly; **do not source it** with `source` or `.`.
Installation alone does not turn on the refresh schedule. Run one of the
commands below to start or update it. The command exits after configuring
systemd; you do not need to keep a terminal open or run the script in the
background.

`3000` seconds is 50 minutes. `30` is useful for a short test; restore the
normal interval afterward. These examples use the executable's full path,
so no `PATH` changes or shell sourcing are required. If `~/.local/bin` is on
your `PATH`, you can use `bedrock-codex-interval` as the command name instead.

If you already have a refresh timer, pass its name to update that timer
instead of creating a second schedule:

```bash
~/.local/bin/bedrock-codex-interval 3000 my-existing-refresh.timer
systemctl --user list-timers my-existing-refresh.timer --all
```

This restarts the named timer with the requested interval. If you do not
already have a refresh timer, create and start the persistent default timer:

```bash
~/.local/bin/bedrock-codex-interval 3000
systemctl --user list-timers codex-bedrock-refresh.timer --all
```

Choose one schedule; do not run both an existing and the default timer.
The default command creates, enables, and starts `codex-bedrock-refresh.timer`
and a oneshot service that runs the installed Linux `bedrock-codex --auth`
launcher. Unit files live in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/`.
Later calls change the interval using a timer drop-in without replacing
the service.

Updating an existing timer preserves its service and enablement. A transient
timer remains transient; changing its interval does not make it survive a
reboot. The default timer is enabled for future user-manager sessions.

All Linux operations use `systemctl --user`: no `sudo`, no system-wide units,
and no changes to other Linux accounts. Timers run while your user manager is
running; this script does not enable lingering to keep it running after logout
(run `loginctl enable-linger "$USER"` yourself for that). The authentication
command must already work non-interactively.

To inspect or stop the default Linux timer:

```bash
systemctl --user list-timers codex-bedrock-refresh.timer --all
journalctl --user -u codex-bedrock-refresh.service
systemctl --user disable --now codex-bedrock-refresh.timer
```

## Environment overrides

| Variable | Used by | Default |
| --- | --- | --- |
| `CLAUDE_BEDROCK_PROFILE` | `bedrock-claude` | `claude_prod` (source profile) |
| `CLAUDE_BEDROCK_REFRESH_THRESHOLD` | `bedrock-claude` | `300` (refresh when fewer seconds remain) |
| `CODEX_BEDROCK_PROFILE` | `bedrock-codex`, `codex-bedrock-creds` | `codex_prod` (source profile) |
| `CODEX_BEDROCK_REFRESH_THRESHOLD` | `bedrock-codex` | `300` seconds; `codex-bedrock-creds` sets it for the call it makes |
| `CODEX_BEDROCK_REFRESH_MIN` | `codex-bedrock-creds` | `6` (minutes left that trigger a refresh in `credential_process` mode) |
| `CODEX_BEDROCK_FROM_CREDS` | `bedrock-codex` | set to `1` by `codex-bedrock-creds`; skips the timer install |
| `AWS_SHARED_CREDENTIALS_FILE` | all | `~/.aws/credentials` |
| `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR` | launchers | systemd unit directory, Linux lock file, macOS lock directory |

The Codex runtime profile (`codex_bedrock`), region (`us-east-1`) and provider name
(`amazon-bedrock-runtime`) are constants at the top of `scripts/bedrock-codex`; edit them
there if your setup differs.

## Troubleshooting

- **Logs.** `codex-bedrock-creds` appends to `~/.codex/logs/bedrock-refresh.log` (every refresh
  attempt, the launcher's exit code and the tail of its output). Timer runs: see the table above.
- **Re-run the refresh by hand.** `bedrock-claude --auth --force` or `bedrock-codex --auth --force`
  prints exactly what failed. A `macaw-auth` error means the login itself needs attention
  (keyring unlocked, VPN, password changed).
- **`systemctl --user` fails / "No systemd user manager; refresh timer not installed".**
  The timer needs a systemd user session. On a remote or container login, check
  `systemctl --user show-environment`; enable linger (`loginctl enable-linger "$USER"`) so the
  user manager and timer survive logout.
- **`'<command>' not found on PATH`.** Install it, or put its directory on `PATH`. The timer and the
  LaunchAgent run with `~/.local/bin:/usr/local/bin:/usr/bin:/bin` (macOS adds `/opt/homebrew/bin`);
  `macaw-auth` and `aws` must be reachable from there.
- **Codex gets expired credentials.** Run the profile directly: `~/.local/bin/codex-bedrock-creds`
  should print JSON. If it exits 1, run `bedrock-codex --auth --force` and read the log above.
- **Stale lock on macOS.** A lock directory named `bedrock-<tool>-refresh-<uid>.lock.d` under
  `$TMPDIR` is removed when its owner exits and broken automatically after 5 minutes.
- **A stale `AWS_BEARER_TOKEN_BEDROCK`** overrides AWS credentials; `bedrock-codex` unsets it.

## Tests

```bash
bash tests/test-bedrock-launchers.sh        # launchers and codex-bedrock-creds, macOS and Linux paths
bash tests/test-bedrock-codex-interval.sh   # the two interval helpers
```

Everything external (`macaw-auth`, `aws`, `systemctl`, `launchctl`, `osascript`, `codex`, `claude`,
`uname`, ...) is faked through `PATH`, in a temporary directory with a fake `HOME`, so nothing
real is refreshed, installed or launched. The macOS path is tested on any host the same way.
The tests use only `grep -E`, `python3` and standard shell tools.

## Do not commit secrets

Do not commit AWS credentials, Codex authentication files, `.env` files, session history,
logs, or personal configuration files to this repository.
