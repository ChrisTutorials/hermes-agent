"""The respawn guard's ``active_pr`` branch must be scoped to the card it guards.

``active_pr`` exists to stop a worker from opening a DUPLICATE PR for its own
card. Measured on the live ``default`` board it was neither PR-aware nor
state-aware: it fired on ANY GitHub PR URL in ANY recent comment, from ANY
author. ``t_321d52c8`` (a card about PR #3933) was held for 21 consecutive
dispatch ticks by comments naming #3949 and #3952 -- other cards' PRs -- and
the gateway logged ``ready queue non-empty ... 0 workers spawned.

The guard is a duplicate-work guard, so "a PR URL exists somewhere on this
card" is not evidence. What is evidence is "a profile that works THIS card
announced a URL for a PR THIS card is about".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

REPO = "https://github.com/ChrisTutorials/thistletide-gd/pull"


@pytest.fixture
def board(tmp_path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn, *, title, author=None, runs=(), assignee="implementer",
          idempotency_key=None):
    """A ready card that ran under ``author`` (its assignee unless given)."""
    tid = kb.create_task(
        conn, title=title, assignee=assignee, idempotency_key=idempotency_key,
    )
    if author:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, ?, 'completed', 1)",
                (tid, author),
            )
    return tid


def _comment(conn, tid, author, body):
    kb.add_comment(conn, tid, author=author, body=body)


# --- (a) the PR must be one the CARD names -------------------------------------

def test_other_cards_open_pr_does_not_hold_this_card(board) -> None:
    """THE LIVE BUG: #3933's card held by a comment about #3952."""
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "implementer", f"Supersedes #3949; new PR {REPO}/3952")

        assert kbd.check_respawn_guard(conn, tid) is None


def test_own_pr_named_only_in_the_idempotency_key_is_scoped(board) -> None:
    """The native key shape ``github:<o>/<r>:pr:<n>[:suffix]`` scopes too."""
    with kbc.connect() as conn:
        tid = _card(conn, title="republish the clean rebuild",
                    author="implementer",
                    idempotency_key="github:ChrisTutorials/thistletide-gd:pr:3901:republish")
        _comment(conn, tid, "implementer", f"new attempt {REPO}/3966")

        assert kbd.check_respawn_guard(conn, tid) is None


def test_legacy_pr_landing_key_is_scoped(board) -> None:
    """The legacy ``pr-landing:<short>#<n>`` key scopes too."""
    with kbc.connect() as conn:
        tid = _card(conn, title="land the parser fix", author="implementer",
                    idempotency_key="pr-landing:thistletide-gd#3956")
        _comment(conn, tid, "implementer", f"see also {REPO}/3949")

        assert kbd.check_respawn_guard(conn, tid) is None


# --- (b) only a profile that works the card is worker evidence -----------------

def test_coordinator_note_about_the_cards_own_pr_does_not_hold(board) -> None:
    """A sweep note is not "a prior worker already opened a PR".

    The live card was held for 21 ticks while coordinator sweeps discussed its
    PR number; a coordinator profile never runs cards, so its comment is not
    evidence that a worker already did the work.
    """
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "coordinator",
                 f"Coordinator sweep 51: the card is still blocked on {REPO}/3933")

        assert kbd.check_respawn_guard(conn, tid) is None


def test_automation_author_cannot_re_arm_the_guard(board) -> None:
    """Only the card's own worker cohort is evidence; a bystander is not."""
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "some-other-profile", f"FYI {REPO}/3933 landed")

        assert kbd.check_respawn_guard(conn, tid) is None


# --- the guard still works ----------------------------------------------------

def test_worker_naming_the_cards_own_pr_is_still_held(board) -> None:
    """SAFETY: the duplicate-work protection must survive the narrowing."""
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "implementer", f"opened {REPO}/3933 for review")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_worker_naming_the_cards_own_pr_from_the_key_is_held(board) -> None:
    with kbc.connect() as conn:
        tid = _card(conn, title="land the parser fix", author="implementer",
                    idempotency_key="pr-landing:thistletide-gd#3956")
        _comment(conn, tid, "implementer", f"branch pushed, {REPO}/3956")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_card_naming_no_pr_stays_fail_closed(board) -> None:
    """No PR to compare against -> keep the guard, do not open it by accident.

    A worker may have opened a PR the card text never mentions; re-spawning
    that card would duplicate the work. Only the author is checked here.
    """
    with kbc.connect() as conn:
        tid = _card(conn, title="refactor the dispatch loop", author="implementer")
        _comment(conn, tid, "implementer", f"draft up at {REPO}/3970")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_coordination_note_cannot_hold_a_card_naming_no_pr(board) -> None:
    """The author rule holds even where the PR number cannot be compared."""
    with kbc.connect() as conn:
        tid = _card(conn, title="refactor the dispatch loop", author="implementer")
        _comment(conn, tid, "coordinator", f"related work at {REPO}/3970")

        assert kbd.check_respawn_guard(conn, tid) is None


def test_mixed_comments_hold_only_on_the_worker_one(board) -> None:
    """Scoping is per comment, not per card: noise above, evidence below."""
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "coordinator", f"sweep mentions {REPO}/3933")
        _comment(conn, tid, "implementer", f"unrelated repo PR {REPO}/3949")
        _comment(conn, tid, "implementer", f"and mine: {REPO}/3933")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_handoff_still_lifts_a_scoped_hold(board) -> None:
    """#111910 survives narrowing: a handoff after the PR comment still wins."""
    with kbc.connect() as conn:
        tid = _card(conn, title="thistletide PR #3933 head: fix the registry half",
                    author="implementer")
        _comment(conn, tid, "implementer", f"opened {REPO}/3933 for review")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 60 "
                "WHERE task_id = ?", (tid,),
            )
        assert kb.assign_task(conn, tid, "closer") is True

        assert kbd.check_respawn_guard(conn, tid) is None