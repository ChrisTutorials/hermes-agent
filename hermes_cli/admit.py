"""Heavyweight task admission command for cross-project resource control.

Serializes heavy resource phases (compilation, heavy tests, Godot test runners)
across agents using a cooperative file lock and live system pressure sampling.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl  # windows-footgun: ok
except ImportError:
    fcntl = None  # type: ignore[assignment]

# Default timeout to acquire the admission lock before failing closed with 75 (resource blocked)
DEFAULT_ADMISSION_WAIT_SECONDS = 60.0
EXIT_CODE_RESOURCE_BLOCKED = 75

logger = logging.getLogger(__name__)


def _read_proc_fields(path: str, wanted: Mapping[str, str]) -> dict[str, int]:
    found: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    if key in wanted:
                        found[wanted[key]] = int(parts[1].split()[0])
                        if len(found) == len(wanted):
                            break
    except (OSError, ValueError, IndexError):
        return {}
    return found


def sample_system_pressure() -> dict[str, Any]:
    """Sample live memory and pressure metrics from /proc without third-party dependencies."""
    sample: dict[str, Any] = {"available": False}
    mem = _read_proc_fields("/proc/meminfo", {"MemTotal": "mem_total_kib", "MemAvailable": "mem_available_kib"})
    if mem:
        sample["available"] = True
        sample.update(mem)

    # PSI metrics if available
    for p_name in ("cpu", "memory", "io"):
        psi_file = f"/proc/pressure/{p_name}"
        if os.path.exists(psi_file):
            try:
                with open(psi_file, encoding="utf-8-sig") as f:
                    for line in f:
                        if line.startswith("some ") or line.startswith("full "):
                            prefix = f"{p_name}_{line.split()[0]}"
                            for token in line.split()[1:]:
                                if token.startswith("avg10="):
                                    sample[f"{prefix}_avg10"] = float(token.split("=")[1])
            except (OSError, ValueError):
                pass
    return sample


def check_host_pressure_level(sample: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Return (is_ok, reason). Evaluates if system is currently overloaded."""
    if sample is None:
        sample = sample_system_pressure()
    if not sample.get("available"):
        return True, "pressure unknown (fail open)"

    # Memory check: require at least 512MB available for heavy phase
    available_kib = sample.get("mem_available_kib", 0)
    if available_kib > 0 and available_kib < (512 * 1024):
        return False, f"available memory critical: {available_kib // 1024}MB < 512MB"

    # CPU/IO extreme saturation check (e.g., avg10 > 85.0 on PSI indicates severe stall)
    if sample.get("memory_full_avg10", 0.0) > 40.0:
        return False, f"memory stall pressure critical (full avg10: {sample['memory_full_avg10']}%)"

    return True, "ok"


def run_admitted_command(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_ADMISSION_WAIT_SECONDS,
    lock_path: Path | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Acquire the shared host-wide heavy lock and execute the given command.

    If timeout expires or system is overloaded, returns EXIT_CODE_RESOURCE_BLOCKED (75).
    """
    if not cmd:
        return 0

    # Nested admission detection: if parent already holds admission, proceed without re-locking
    if os.environ.get("HERMES_ADMISSION_ACTIVE") == "1":
        proc = subprocess.run(cmd, cwd=cwd, env=env)
        return proc.returncode

    if lock_path is None:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        user_id = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
        lock_path = Path(runtime_dir) / f"hermes-heavy-admission-{user_id}.lock"

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start_time = time.monotonic()
    fd = None

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        logger.error("Could not open admission lock file %s: %s", lock_path, e)
        # Fail-open if lock file cannot be created
        proc = subprocess.run(cmd, cwd=cwd, env=env)
        return proc.returncode

    locked = False
    while time.monotonic() - start_time < timeout:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            break
        except (BlockingIOError, OSError):
            time.sleep(0.5)

    if not locked:
        os.close(fd)
        sys.stderr.write(
            f"hermes admit: RESOURCE BLOCKED - could not acquire heavy lock within {timeout}s\n"
        )
        return EXIT_CODE_RESOURCE_BLOCKED

    try:
        ok, reason = check_host_pressure_level()
        if not ok:
            sys.stderr.write(f"hermes admit: RESOURCE BLOCKED - host pressure unacceptable: {reason}\n")
            return EXIT_CODE_RESOURCE_BLOCKED

        child_env = dict(os.environ if env is None else env)
        child_env["HERMES_ADMISSION_ACTIVE"] = "1"

        # Execute command, propagating signals properly
        # ponytail: single-slot mutual exclusion lock serializes heavy phases across projects.
        # Upgrade path: multi-slot semaphore when machine memory expands or cgroups quota is set.
        proc = subprocess.Popen(cmd, cwd=cwd, env=child_env)

        def _forward_sig(signum, frame):
            try:
                proc.send_signal(signum)
            except OSError:
                pass

        old_term = signal.signal(signal.SIGTERM, _forward_sig)
        old_int = signal.signal(signal.SIGINT, _forward_sig)
        try:
            return proc.wait()
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)

    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes admit",
        description="Gate heavyweight processes (builds/heavy tests) via host-wide admission",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_ADMISSION_WAIT_SECONDS,
        help="Max wait in seconds before returning exit 75 (resource blocked)",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=None,
        help="Override path to admission lock file",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command and arguments to run under admission",
    )

    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        parser.print_help(sys.stderr)
        return 2

    return run_admitted_command(cmd, timeout=args.timeout, lock_path=args.lock_path)


def admit_command(args: argparse.Namespace) -> int:
    """Forwarder for hermes admit subcommand."""
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        sys.stderr.write("hermes admit: missing command to run\n")
        return 2
    return run_admitted_command(cmd, timeout=args.timeout, lock_path=args.lock_path)


if __name__ == "__main__":

    sys.exit(main())
