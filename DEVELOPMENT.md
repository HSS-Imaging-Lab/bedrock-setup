# Development

The launchers in `scripts/` are copy-installed shell and Python tools (see README.md). This
repo has a Python test harness only; there is no package to build.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 from `.python-version`)
- `bash`, `make`, `git`
- For the live tier only: the `aws` and `codex` CLIs, and `macaw-auth`

## Setup

```bash
make setup    # uv sync --locked, then install the git hooks
```

## Running locally

```bash
make check    # lint, offline tests, audit: what CI runs
make test     # offline tests only
```

## Commands

| Command | What it does |
|---|---|
| `make help` | List every target |
| `make setup` | Install locked dependencies and git hooks |
| `make fmt` | Format and autofix |
| `make lint` | ruff, mypy, ratchet, import checks |
| `make test` | Offline tests (same as `test-unit`) |
| `make test-live` | Live Bedrock checks; `PROFILE=<name>` overrides `codex_prod` |
| `make audit` | pip-audit and gitleaks |
| `make check` | lint, test, audit (not `test-live`) |
| `make clean` | Remove caches |

## Test tiers

- **Unit** (`tests/unit/`, `make test`): checks the model catalog against the launcher, and runs
  the two bash suites (`tests/test-*.sh`). Network is blocked.
- **Live** (`tests/e2e/`, `make test-live`): for each catalog model, checks that the Bedrock
  inference profile is ACTIVE and that `codex exec` answers. It calls real AWS and costs a few
  tokens. A bare `pytest` skips it (`-m "not live"`).

## Test data

No test data files. `tests/fixtures/` holds fakes for the bash suites; `templates/` holds the
model catalog the tests read.

## Troubleshooting

- **Live tier fails at the first step:** the profile's keys expired. Run
  `macaw-auth login codex_prod --target-profile codex_prod` (use your profile name), then retry.
- **`Model metadata for ... not found` in live output:** the slug is missing from
  `templates/global-gpt6-models.json`.
- **`make setup` fails on a missing lock:** run `uv lock` and commit `uv.lock`.
