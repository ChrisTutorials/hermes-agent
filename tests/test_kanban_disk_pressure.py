"""Tests for disk backpressure in the kanban dispatcher (2026-10-01).

A worker that cannot write its transcript exits and loses its verdict --
t_dd15d08f died that way. Memory already had a guard; disk had none. These
tests pin the thresholds and, critically, that a statfs failure does NOT
brick dispatch.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import kanban_db_dispatch as d  # noqa: E402

FAILED = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'} {name}")
    if not cond:
        FAILED.append(name)


class FakeVfs:
    """Minimal statvfs stand-in; f_bavail is in f_frsize units."""

    def __init__(self, free_mib):
        self.f_frsize = 4096
        self.f_bavail = int(free_mib * 1024 * 1024 / self.f_frsize)


real_statvfs = os.statvfs


def with_free(free_mib):
    os.statvfs = lambda p: FakeVfs(free_mib)


os.statvfs = real_statvfs

# Thresholds must be ordered and nonzero, or "critical" is unreachable.
check("elevated above critical", d.DISK_ELEVATED_MIB > d.DISK_CRITICAL_MIB > 0)

# The three bands.
with_free(d.DISK_ELEVATED_MIB + 10_000)  # comfortably above the elevated band
check("plenty free -> ok", d._disk_pressure_level("/tmp") == "ok")

with_free(int(d.DISK_CRITICAL_MIB + (d.DISK_ELEVATED_MIB - d.DISK_CRITICAL_MIB) / 2))
check("between thresholds -> elevated", d._disk_pressure_level("/tmp") == "elevated")

with_free(d.DISK_CRITICAL_MIB - 1)
check("below critical -> critical", d._disk_pressure_level("/tmp") == "critical")

with_free(0)
check("completely full -> critical", d._disk_pressure_level("/tmp") == "critical")

# The real box right now: ~282G free, must not trip.
os.statvfs = real_statvfs
live = d._disk_pressure_level()
check(f"live disk reads ok, not suppressed (got {live})", live == "ok")

# A statfs failure must never brick dispatch -- same rule as memory "unknown".
def boom(_p):
    raise OSError("simulated statfs failure")


os.statvfs = boom
check("statfs failure -> unknown", d._disk_pressure_level("/tmp") == "unknown")

# DispatchResult carries the reason so suppression reporting can name it.
r = d.DispatchResult()
check("disk_pressure defaults to None", r.disk_pressure is None)
r.disk_pressure = "critical"
line = d.describe_suppression([r])
check(f"suppression names disk_pressure (got {line!r})", "disk_pressure=critical" in line)

os.statvfs = real_statvfs

print()
if FAILED:
    print(f"{len(FAILED)} failure(s): " + ", ".join(FAILED))
    raise SystemExit(1)
print("all tests passed")
