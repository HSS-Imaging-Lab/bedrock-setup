"""Run the existing bash test suites under pytest."""

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

SUITES = ["test-bedrock-launchers.sh", "test-bedrock-codex-interval.sh"]
TAIL_LINES = 40


@pytest.mark.timeout(330)  # the launcher suite has its own 300 s watchdog
@pytest.mark.parametrize("suite", SUITES)
def test_shell_suite_passes(repo_root: Path, tmp_path: Path, suite: str) -> None:
    # Output goes to a file, not a pipe: the suites leave background helpers that keep a pipe
    # open for minutes after bash exits, and reading a pipe would wait for them.
    log = tmp_path / "output.log"
    with log.open("wb") as sink:
        result = subprocess.run(
            ["bash", str(repo_root / "tests" / suite)],
            cwd=repo_root,
            stdout=sink,
            stderr=subprocess.STDOUT,
            check=False,
        )
    tail = "\n".join(log.read_text(errors="replace").splitlines()[-TAIL_LINES:])
    assert result.returncode == 0, f"{suite} exited {result.returncode}:\n{tail}"
