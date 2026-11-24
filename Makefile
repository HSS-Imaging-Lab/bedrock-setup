.DEFAULT_GOAL := help
.PHONY: help setup fmt lint test test-unit test-live audit check clean

PROFILE ?= codex_prod

help: ## List every target
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

setup: ## Deps and git hooks
	uv sync --locked
	uv run prek install

fmt: ## Format and autofix
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Every lint, type, and ratchet check
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	python3 scripts/check_ratchet.py
	python3 scripts/check_imports.py

test-unit: ## Unit tests (includes the bash suites), network blocked
	uv run pytest tests/unit --disable-socket --allow-unix-socket

test: test-unit ## All offline tests

test-live: ## Live Bedrock checks per catalog model; needs fresh keys (PROFILE=codex_prod)
	BEDROCK_LIVE_PROFILE=$(PROFILE) uv run pytest tests/e2e -m live -p no:randomly

audit: ## Dependency vulnerabilities and committed secrets
	uv run pip-audit
	uv run prek run gitleaks --all-files

check: lint test audit ## Everything CI runs (not test-live)

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage dist
