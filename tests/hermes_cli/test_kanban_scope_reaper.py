"""Orphan worker-scope reaper.

Observed on the live box: ``hermes-worker-kanban-t_3be306c5-run-778.scope``
held 99 tasks and 334% CPU for a card that no longer existed in the database,
and ``-run-776.scope`` held 33 tasks for an archived card.
``systemd-run --collect`` cannot reap either: --collect only fires once every
process in the scope exits, and a hung worker never exits.

What matters most is the safety property -- a worker legitimately in flight is
never touched -- so that is tested first and most explicitly.
"""
from __future__ import annotations

import subprocess
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


@pytest.fixture
def units(monkeypatch: pytest.MonkeyPatch):
    """Serve a systemctl list-units table; yields a setter for unit names."""
    table = {"names": []}

    def fake_run(cmd, **kwargs):
        if "list-units" in cmd:
            listing = "".join(f"{n} loaded active running w\n" for n in table["names"])
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        raise AssertionError(f"unexpected systemctl call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    table["set"] = lambda *names: table.__setitem__("names", list(names))
    return table


def _claim(conn, title: str = "card") -> tuple[str, int]:
    """Create + claim a card, returning (task_id, run_id)."""
    tid = kb.create_task(conn, title=title, assignee="implementer")
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    return tid, int(run_id)


def test_running_card_scope_is_never_stopped(kanban_home: Path, units) -> None:
    """SAFETY: real work in flight is left strictly alone."""
    with kbc.connect() as conn:
        tid, run_id = _claim(conn)
        units["set"](f"hermes-worker-kanban-{tid}-run-{run_id}.scope")

        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == []
        assert stopped == []


def test_blocked_card_scope_is_left_alone(kanban_home: Path, units) -> None:
    """blocked is not terminal: a worker may legitimately be unwinding."""
    with kbc.connect() as conn:
        tid, run_id = _claim(conn)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        conn.commit()
        units["set"](f"hermes-worker-kanban-{tid}-run-{run_id}.scope")

        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == []
        assert stopped == []


def test_review_card_scope_is_left_alone(kanban_home: Path, units) -> None:
    """review hands off to another worker; not terminal either."""
    with kbc.connect() as conn:
        tid, run_id = _claim(conn)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        units["set"](f"hermes-worker-kanban-{tid}-run-{run_id}.scope")

        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == []
        assert stopped == []


def test_done_card_scope_is_reaped(kanban_home: Path, units) -> None:
    """LEAK: a completed card must not keep a scope alive."""
    with kbc.connect() as conn:
        tid, run_id = _claim(conn)
        kb.complete_task(conn, tid, summary="shipped it")
        unit = f"hermes-worker-kanban-{tid}-run-{run_id}.scope"
        units["set"](unit)

        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == [unit]
        assert stopped == [unit]


def test_missing_card_scope_is_reaped(kanban_home: Path, units) -> None:
    """LEAK: the exact live incident -- the card is gone from the database."""
    unit = "hermes-worker-kanban-t_deadbeef-run-776.scope"
    units["set"](unit)

    with kbc.connect() as conn:
        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == [unit]
        assert stopped == [unit]


def test_superseded_run_scope_is_reaped(kanban_home: Path, units) -> None:
    """A scope for an OLD run of a still-open card is still an orphan."""
    with kbc.connect() as conn:
        tid, run_id = _claim(conn)
        stale = f"hermes-worker-kanban-{tid}-run-{run_id - 1}.scope"
        live = f"hermes-worker-kanban-{tid}-run-{run_id}.scope"
        units["set"](stale, live)

        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == [stale]
        assert stopped == [stale]


def test_unrecognised_unit_names_are_never_stopped(kanban_home: Path, units) -> None:
    """Anything not matching our exact pattern is off limits."""
    units["set"](
        "hermes-worker-kanban-run-5.scope",
        "hermes-gateway.service",
        "hermes-worker-kanban-t_abcDE-run-7.scope",
    )

    with kbc.connect() as conn:
        stopped: list[str] = []
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=stopped.append) == []
        assert stopped == []


def test_stop_failure_does_not_abort_the_sweep(kanban_home: Path, units) -> None:
    """One bad stop skips that scope, not the rest of the tick."""
    good = "hermes-worker-kanban-t_aaa11111-run-1.scope"
    bad = "hermes-worker-kanban-t_bbb22222-run-2.scope"
    units["set"](good, bad)

    def flaky(unit: str) -> None:
        if unit == bad:
            raise RuntimeError("systemctl exploded")

    with kbc.connect() as conn:
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=flaky) == [good]


def test_systemctl_failure_returns_empty(kanban_home: Path, monkeypatch) -> None:
    """No user bus is a host condition: reaping nothing is correct, not an error."""
    def boom(cmd, **kwargs):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(subprocess, "run", boom)
    with kbc.connect() as conn:
        assert kbd.reap_orphan_worker_scopes(conn, stop_fn=lambda u: None) == []