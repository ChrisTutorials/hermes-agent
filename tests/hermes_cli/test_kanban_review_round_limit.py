"""Rework circuit breaker: a review loop must converge or escalate.

Measured over 5 days of board data:
  clean cards (0 review rounds): 106 min avg, 87% completed
  rework cards (>=1 round):       502 min avg, 70% completed
  6 cards reached 3+ rounds and never completed; worst spun to 11.

So request_changes() caps rounds and blocks for a human instead of requeueing
forever. These tests pin both halves: the loop stops at the cap, and a card
under the cap is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _review_round(conn, tid: str) -> str:
    """Drive one full review round that ends in changes_requested."""
    kb.request_review(
        conn, tid, summary="impl done", reviewer="reviewer",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    # review -> running is the reviewer's claim, not the worker's claim_task.
    assert kb.claim_review_task(conn, tid) is not None
    return tid


def _status(conn, tid: str) -> str:
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()["status"]


def test_round_under_limit_is_unaffected(kanban_home: Path) -> None:
    """A first change request still hands back to the implementer."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix a thing", assignee="implementer")
        kb.claim_task(conn, tid)
        _review_round(conn, tid)

        ok, implementer = kb.request_changes(
            conn, tid, reason="needs a tweak",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert ok is True
        assert implementer == "implementer"
        assert _status(conn, tid) != "blocked"


def test_third_change_request_blocks_for_human(kanban_home: Path) -> None:
    """Round 3 hits DEFAULT_REVIEW_ROUND_LIMIT and stops the loop."""
    assert kb.DEFAULT_REVIEW_ROUND_LIMIT == 3

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spins forever", assignee="implementer")
        kb.claim_task(conn, tid)

        # Rounds 1 and 2: normal requeue.
        for _ in range(2):
            _review_round(conn, tid)
            ok, _ = kb.request_changes(
                conn, tid, reason="still wrong",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
            assert ok is True
            assert _status(conn, tid) != "blocked"

        # Round 3: the cap trips.
        _review_round(conn, tid)
        ok, reason = kb.request_changes(
            conn, tid, reason="still wrong",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert ok is False
        assert "review round limit" in (reason or "")
        assert _status(conn, tid) == "blocked"

        # No active run is left holding a slot.
        assert kb.get_task(conn, tid).current_run_id is None


def test_blocked_card_is_sticky_not_auto_requeued(kanban_home: Path) -> None:
    """The dispatcher must not resurrect a capped card on its next sweep."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spins forever", assignee="implementer")
        kb.claim_task(conn, tid)
        for _ in range(kb.DEFAULT_REVIEW_ROUND_LIMIT):
            _review_round(conn, tid)
            kb.request_changes(
                conn, tid, reason="nope",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
        assert _status(conn, tid) == "blocked"

        # kanban_block is what makes the block sticky.
        blocks = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'kanban_block'",
            (tid,),
        ).fetchall()
        assert len(blocks) == 1
        payload = json.loads(blocks[0]["payload"])
        assert payload.get("source") == "review_round_limit"


def test_config_override_changes_the_ceiling(kanban_home: Path) -> None:
    """kanban.review_round_limit is the one knob, no per-card column."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="genuinely hard", assignee="implementer")
        assert kb.review_round_limit(conn, tid) == kb.DEFAULT_REVIEW_ROUND_LIMIT

        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            kb, "_review_round_limit_override", lambda: 6, raising=True
        )
        try:
            assert kb.review_round_limit(conn, tid) == 6

            kb.claim_task(conn, tid)
            for _ in range(3):
                _review_round(conn, tid)
                kb.request_changes(
                    conn, tid, reason="nope",
                    expected_run_id=kb.get_task(conn, tid).current_run_id,
                )
            # 4 rounds in, still under its own ceiling of 6: not blocked.
            assert _status(conn, tid) != "blocked"
        finally:
            monkey.undo()


def test_crashed_review_does_not_consume_a_round(kanban_home: Path) -> None:
    """Only verdicts count. A crashed reviewer produced no feedback."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flaky reviewer", assignee="implementer")
        kb.claim_task(conn, tid)

        # A review run that crashes rather than returning a verdict.
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at) "
            "VALUES (?, 'running', 'crashed', strftime('%s','now'))",
            (tid,),
        )
        conn.commit()
        assert kb._review_round_count(conn, tid) == 1  # in-flight round only