"""Tests for the dev install script (dry run only; nothing is copied)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "install_dev.sh"
TARGET = "/config/custom_components/tidepool"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_no_arguments_exits_non_zero() -> None:
    result = _run()
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_too_many_arguments_exits_non_zero() -> None:
    assert _run("ha-one", "ha-two").returncode != 0
    assert _run("--dry-run").returncode != 0
    assert _run("--dry-run", "ha-one", "ha-two").returncode != 0


def test_dry_run_names_only_expected_paths_and_does_not_restart() -> None:
    result = _run("--dry-run", "ha-example")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "ha-example" in out
    assert "custom_components/tidepool" in out
    assert "__pycache__" in out  # excluded
    assert f"rm -rf {TARGET}" in out
    # Only that single exact path is ever removed.
    assert out.count("rm ") == 1
    assert f"rm -rf {TARGET} &&" in out
    # Nothing restarts, reloads or reboots Home Assistant; the one mention of
    # restarting is the reminder that it was NOT done.
    for line in out.splitlines():
        if "restart" in line.lower():
            assert "NOT restarted" in line
    for word in ("reboot", "reload", "ha core", "systemctl", "docker", "supervisor"):
        assert word not in out.lower()
    assert "/config/configuration.yaml" not in out
