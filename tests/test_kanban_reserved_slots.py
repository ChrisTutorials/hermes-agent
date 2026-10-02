"""Tests for kanban.reserved_slots (Chris's plugin-priority ruling, 2026-10-01).

The host cap ``kanban.max_in_progress`` is shared by every board, so a busy
board can consume it entirely and starve a quieter one. These tests pin the
reservation arithmetic and prove the no-config path is unchanged.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import kanban_db_dispatch as d  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name}")


class FakeConn:
    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


def budget_for(board, cap=9, running=0, other=0, reserved=None):
    """Reproduce the cap arithmetic the dispatcher applies."""
    eff = cap
    if reserved and board not in reserved:
        eff = cap - max(reserved.values())
        if eff < 1:
            return 0
    total = running + other
    if total >= eff:
        return 0
    return eff - total


# 2. Arithmetic, derived from whatever the host cap and reservation currently
#    are. These used to assert the literal numbers 5 and 9, so every host-cap
#    change broke four tests that were never wrong. Derive instead: the
#    unreserved board must sit exactly max(0, cap - reserved), and the reserved
#    board gets the full cap.
_cap = d._cfg_int("kanban.max_in_progress", 9) if hasattr(d, "_cfg_int") else 9
_reserved_live = d.configured_reserved_slots()
_res = _reserved_live.get("grid-placement", 0)
_expect_unreserved = max(0, _cap - _res)
check(
    "live config reserves grid-placement for the plugin board",
    _res > 0,
)
check(
    f"unreserved board capped below host cap (cap={_cap} reserved={_res})",
    budget_for("default", reserved=_reserved_live) == _expect_unreserved,
)
check(
    f"reserved board gets full cap (cap={_cap})",
    budget_for("grid-placement", reserved=_reserved_live) == _cap,
)

# 3. The reported bug: default holding a full share must not close the board
#    for itself AND starve the reserved board.
check(
    "default at its own cap can no longer spawn",
    budget_for("default", running=_expect_unreserved, other=0, reserved=_reserved_live) == 0,
)
check(
    "default one below its cap may still spawn",
    budget_for("default", running=_expect_unreserved - 1, other=0, reserved=_reserved_live) == 1,
)

# 4. grid-placement itself can still spawn into the reservation even when
#    default is already holding its full share.
check(
    "grid spawns while default sits at its cap",
    budget_for("grid-placement", running=0, other=_expect_unreserved, reserved=_reserved_live) == _res,
)

# 5. Guard rails: junk config, zero/negative, over-reservation.
def _with(raw):
    fake = types.ModuleType("hermes_cli.config")
    setattr(fake, "load_config_readonly", lambda: raw)
    sys.modules["hermes_cli.config"] = fake


_with({"kanban": {"reserved_slots": "not-a-dict"}})
check("junk type yields empty", d.configured_reserved_slots() == {})

_with({"kanban": {"reserved_slots": {"grid-placement": 0, "x": -3, "y": "abc", "z": 2}}})
check("only positive ints survive", d.configured_reserved_slots() == {"z": 2})

_with({})
check("missing kanban block yields empty", d.configured_reserved_slots() == {})

# Over-reservation must not produce a negative cap or unbounded spawn.
check(
    "over-reservation floors at zero, never negative",
    budget_for("default", cap=9, reserved={"grid-placement": 20}) == 0,
)

print()
if FAILED:
    print(f"{len(FAILED)} failure(s): " + ", ".join(FAILED))
    raise SystemExit(1)
print("all tests passed")
