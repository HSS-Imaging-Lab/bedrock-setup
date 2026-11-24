# Standard deviations

This repo is a set of copy-installed shell and Python tools, not a package. The owner chose a
test harness only (T1). Each line below is kept on purpose.

- `src/*`: no src package; the scripts are copied onto user machines, never imported
- `pyproject.toml:project.dependencies`: no runtime dependencies; the repo ships scripts, not a package
- `pyproject.toml:project.scripts.<package>`: no package, so no console entry point
- `pyproject.toml:build-system.*`: package = false in [tool.uv]; nothing to build
- `pyproject.toml:tool.hatch.*`: no build backend, so no wheel target
- `pyproject.toml:tool.importlinter.*`: no package, so no import contracts to keep
- `pyproject.toml:tool.coverage.*`: no source package to measure and no coverage floor; the tests drive shell scripts through subprocess
- `pyproject.toml:tool.mypy.plugins`: no pydantic in this repo
- `pyproject.toml:tool.mypy.exclude`: no third_party, vendor, or tests/data folders to exclude
- `pyproject.toml:tool.pytest.ini_options.norecursedirs`: no tests/data, third_party, or vendor folders
- `pyproject.toml:tool.pytest.ini_options.asyncio_*`: no async code
- `pyproject.toml:dependency-groups.test`: no pytest-asyncio, pytest-cov, hypothesis, snapshot libraries, or testcontainers: no async code, coverage gate, or services
- `pyproject.toml:dependency-groups.lint`: no import-linter: there is no package for it to check
- `pyproject.toml:tool.ruff.src`: the code is in tests/ and scripts/, not src/
- `pyproject.toml:tool.ruff.extend-exclude`: only tests/fixtures (bash suite fakes) needs excluding; no third_party, vendor, or tests/data
- `pyproject.toml:tool.ruff.lint.per-file-ignores.tests/**`: tests also ignore S603 and S607: they run fixed bash, aws, and codex commands
- `pyproject.toml:tool.mypy.files`: scripts/ is mostly bash; only the three check_*.py copies and tests/ are type-checked, and scripts/codex-bedrock-creds is left out (see below)
- `pyproject.toml:tool.pytest.ini_options.addopts`: adds -m 'not live' so a bare pytest never calls AWS Bedrock
- `pyproject.toml:tool.pytest.ini_options.markers`: adds the live marker for the Bedrock tier
- `make:repos`: no reference repos to fetch
- `make:scripts/fetch_repos.py`: no reference repos to fetch
- `scripts/fetch_repos.py`: no reference repos to fetch
- `scripts/repos.toml`: no reference repos to fetch
- `make:dev`: no server or compose stack
- `make:test-integration`: no adapters against local services; the live tier (make test-live) covers real Bedrock
- `make:coverage`: no coverage floor; see tool.coverage
- `tests/integration/`: no integration tier; the live tier is tests/e2e/
- `tests/fakes/`: the tests use no fakes; the bash suites keep theirs in tests/fixtures/
- `tests/data/`: no test data submodule; the repo holds no data files
- `START_HERE.md`: not added in this task (harness only); follow-up
- `AGENTS.md`: not added in this task (harness only); follow-up
- `CLAUDE.md`: not added in this task (harness only); follow-up
- `CODEOWNERS`: not added in this task (harness only); follow-up
- `GLOSSARY.md`: not added in this task (harness only); follow-up
- `.repos/`: no reference repos (see make:repos)
- `.gitmodules`: no submodules
- `.dockerignore`: no image ships
- `.env.example`: no Settings; the scripts read only the variables README.md documents

Not a bedrock-setup item: `scripts/codex-bedrock-creds` is left out of the ruff and mypy gates on purpose. It has 11 ruff violations (7 D103, 4 UP017) and must keep running on macOS system Python 3.9. Cleaning it up is a follow-up.
