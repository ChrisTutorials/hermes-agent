"""Default max_runtime for cards that set no limit.

Before this, enforce_max_runtime's query filtered on
``max_runtime_seconds IS NOT NULL``. A card with NULL was therefore never
bounded: a worker that hung kept its scope, its slot and its CPU forever. That
is how a scope for an archived card held 334% CPU past its run's end.

The default is config, and 0/absent means "off" so existing behaviour is
unchanged unless an operator opts in.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_card(conn, *, limit=None) -> str:
    tid = kb.create_task(conn, title="hangs forever", assignee="implementer")
    if limit is not None:
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?", (limit, tid)
        )
        conn.commit()
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    # enforce_max_runtime only considers a card with a live worker pid; a real
    # spawn records one, so stand it in as the test process.
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
    conn.commit()
    return tid


def _backdate(conn, tid: str, seconds: int) -> None:
    """Make the active run look like it started `seconds` ago."""
    import time
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = "
        "(SELECT current_run_id FROM tasks WHERE id = ?)",
        (int(time.time()) - seconds, tid),
    )
    conn.commit()


def test_absent_config_leaves_cards_unbounded(kanban_home: Path) -> None:
    """Default off: behaviour is unchanged unless someone opts in."""
    assert kbd._default_max_runtime_seconds() is None


def test_default_limit_times_out_a_null_limit_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix: a card with no explicit limit is still bounded."""
    monkeypatch.setattr(kbd, "_default_max_runtime_seconds", lambda: 600)

    with kbc.connect() as conn:
        tid = _running_card(conn)  # max_runtime_seconds IS NULL
        _backdate(conn, tid, 601)

        killed: list[tuple[int, int]] = []
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == [tid]
        assert killed  # the worker was actually signalled


def test_within_the_default_limit_is_left_alone(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-but-legal run is not evidence of a hang."""
    monkeypatch.setattr(kbd, "_default_max_runtime_seconds", lambda: 3600)

    with kbc.connect() as conn:
        tid = _running_card(conn)
        _backdate(conn, tid, 60)

        killed: list[tuple[int, int]] = []
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == []
        assert killed == []


def test_per_card_limit_overrides_the_default(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit per-card limit wins over the global default."""
    monkeypatch.setattr(kbd, "_default_max_runtime_seconds", lambda: 3600)

    with kbc.connect() as conn:
        tid = _running_card(conn, limit=60)
        _backdate(conn, tid, 61)

        killed: list[tuple[int, int]] = []
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == [tid]
        assert killed


@pytest.mark.parametrize("raw", [0, -1, "0", -900, "", "abc", None, True])
def test_nonpositive_or_junk_config_means_off(
    kanban_home: Path, raw, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that is not a positive integer leaves the default off."""
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config_readonly",
        lambda: {"kanban": {"max_runtime_seconds": raw}},
    )
    assert kbd._default_max_runtime_seconds() is None


@pytest.mark.parametrize("raw,expected", [(600, 600), ("900", 900)])
def test_positive_config_is_honoured(
    kanban_home: Path, raw, expected, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config_readonly",
        lambda: {"kanban": {"max_runtime_seconds": raw}},
    )
    assert kbd._default_max_runtime_seconds() == expected


def test_missing_kanban_config_means_off(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {})
    assert kbd._default_max_runtime_seconds() is None