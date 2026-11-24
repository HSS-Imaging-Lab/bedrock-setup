"""Live checks of every catalog model against real AWS Bedrock (`make test-live`)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

REGION = "us-east-1"
PROVIDER = "amazon-bedrock-runtime"
CATALOG = Path(__file__).resolve().parent.parent.parent / "templates" / "global-gpt6-models.json"
SLUGS: list[str] = [m["slug"] for m in json.loads(CATALOG.read_text(encoding="utf-8"))["models"]]

pytestmark = pytest.mark.live


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=200)


@pytest.fixture(scope="session")
def profile() -> str:
    name = os.environ.get("BEDROCK_LIVE_PROFILE", "codex_prod")
    result = _run(["aws", "sts", "get-caller-identity", "--profile", name])
    if result.returncode != 0:
        pytest.fail(
            f"AWS profile {name} has no valid credentials. "
            f"Fix: macaw-auth login {name} --target-profile {name}\n{result.stderr}"
        )
    return name


@pytest.mark.timeout(240)
@pytest.mark.parametrize("slug", SLUGS)
def test_inference_profile_is_active(profile: str, slug: str) -> None:
    result = _run(
        [
            "aws",
            "bedrock",
            "get-inference-profile",
            "--inference-profile-identifier",
            slug,
            "--region",
            REGION,
            "--profile",
            profile,
            "--output",
            "json",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ACTIVE"


@pytest.mark.timeout(240)
@pytest.mark.parametrize("slug", SLUGS)
def test_codex_exec_answers(profile: str, slug: str, tmp_path: Path) -> None:
    # The model's answer is read from -o, not the output: codex exec echoes the prompt, which
    # holds the word OK.
    answer = tmp_path / "answer.txt"
    result = _run(
        [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "--model",
            slug,
            "-c",
            f'model_provider="{PROVIDER}"',
            "-c",
            f'model_providers.{PROVIDER}.aws.profile="{profile}"',
            "-c",
            f'model_providers.{PROVIDER}.aws.region="{REGION}"',
            "-c",
            f'model_catalog_json="{CATALOG}"',
            "-c",
            'model_reasoning_effort="low"',
            "-o",
            str(answer),
            "Reply with the single word OK.",
        ],
        cwd=tmp_path,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert answer.read_text(encoding="utf-8").strip().rstrip(".").upper() == "OK", output[-2000:]
    assert "Model metadata for" not in output, output[-2000:]
