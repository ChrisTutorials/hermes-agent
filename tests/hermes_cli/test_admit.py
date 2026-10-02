"""Tests for hermes_cli.admit (heavyweight resource admission)."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import admit


def test_admit_runs_simple_command(tmp_path: Path):
    lock_file = tmp_path / "test.lock"
    rc = admit.run_admitted_command([sys.executable, "-c", "import sys; sys.exit(0)"], lock_path=lock_file)
    assert rc == 0


def test_admit_preserves_exit_code(tmp_path: Path):
    lock_file = tmp_path / "test.lock"
    rc = admit.run_admitted_command([sys.executable, "-c", "import sys; sys.exit(42)"], lock_path=lock_file)
    assert rc == 42


def test_admit_timeout_returns_resource_blocked(tmp_path: Path):
    lock_file = tmp_path / "test.lock"
    # Hold the lock externally
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        start = time.monotonic()
        rc = admit.run_admitted_command(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            timeout=1.0,
            lock_path=lock_file,
        )
        elapsed = time.monotonic() - start
        assert rc == admit.EXIT_CODE_RESOURCE_BLOCKED
        assert elapsed >= 0.9
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_admit_nested_invocation_bypasses_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lock_file = tmp_path / "test.lock"
    # Even if locked, if HERMES_ADMISSION_ACTIVE=1, it should execute without waiting
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setenv("HERMES_ADMISSION_ACTIVE", "1")
    try:
        rc = admit.run_admitted_command(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            timeout=0.1,
            lock_path=lock_file,
        )
        assert rc == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_admit_pressure_critical_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    lock_file = tmp_path / "test.lock"
    # Invalidate memory: available < 512MB
    monkeypatch.setattr(
        admit,
        "sample_system_pressure",
        lambda: {"available": True, "mem_total_kib": 16 * 1024 * 1024, "mem_available_kib": 128 * 1024},
    )
    rc = admit.run_admitted_command([sys.executable, "-c", "import sys; sys.exit(0)"], lock_path=lock_file)
    assert rc == admit.EXIT_CODE_RESOURCE_BLOCKED
