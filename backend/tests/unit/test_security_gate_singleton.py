"""Entry 11B5J — two destructive proof runs must not share the proof database.

WHAT HAPPENED. `prove_security_gate.sh` names its database `onyx_sec_proof` —
a constant — and nothing stopped a second instance from using the same one. In
this entry two runs overlapped by accident and the consequences were not subtle:

    DROP TABLE finance.income_source_y2043   blocked on a relation lock for
                                             10m56s behind the other run
    identity.account_lifecycle               one run's claim consumed the
                                             other's fixture row, producing
                                             `assert 'pending' == 'claimed'`
    PD-1 policy present                      reported FAIL — a defect that did
                                             not exist
    trap cleanup EXIT                        either run's exit would DROP the
                                             database the other was using

A gate that can report a defect that does not exist is worse than no gate: the
first response to a red security gate is to go looking for a security hole.

THE GUARD is an exclusive `flock` taken before the EXIT trap is installed, so a
refused run never reaches the cleanup that drops the database. The kernel
releases the lock when the process dies however it dies, including SIGKILL, so
a crashed run cannot wedge the gate.

These tests never touch PostgreSQL: `PGHOST` points at a directory that does
not exist, which is itself part of the proof — a run refused by the lock must
exit before it can connect to anything.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
GATE = BACKEND / "scripts" / "prove_security_gate.sh"

#: The exit code a refused run uses. 69 is EX_UNAVAILABLE from sysexits(3) —
#: distinguishable from 1, which is what a genuine gate failure returns.
REFUSED = 69

#: Nothing in the test may reach a real cluster: the authoritative gate run may
#: be using it, and that is the failure mode under test.
NO_CLUSTER = "/nonexistent-onyx-security-gate"


def _run(lock_path: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ONYX_SEC_PROOF_LOCK": str(lock_path),
        "PGHOST": NO_CLUSTER,
        "PGPORT": "1",
        "PGSUPER": "nobody",
    }
    return subprocess.run(
        [str(GATE)], cwd=BACKEND, env=env, timeout=timeout,
        capture_output=True, text=True, check=False)


def test_a_second_destructive_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    lock_path = tmp_path / "gate.lock"
    holder = open(lock_path, "w")                      # noqa: SIM115
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run(lock_path)

        assert result.returncode == REFUSED, (
            f"a concurrent run was not refused (exit {result.returncode}); "
            f"stdout={result.stdout[-400:]!r} stderr={result.stderr[-400:]!r}")
        assert "SECURITY_GATE_ALREADY_RUNNING" in result.stderr, (
            f"no closed reason code in the refusal: {result.stderr!r}")

        # THE LOAD-BEARING HALF. Exiting non-zero is not enough: the refused
        # run must not have reached the point where it can create, drop or
        # inject into the proof database the other run is using. The banner
        # below is printed immediately before the first `DROP DATABASE`.
        assert "provisioning a disposable database" not in result.stdout, (
            "the refused run reached destructive setup: it can drop the "
            f"database belonging to the run holding the lock\n{result.stdout!r}")
        assert "baseline" not in result.stdout, (
            "the refused run started injecting: "
            f"{result.stdout!r}")
    finally:
        holder.close()

    # And the lock is not a one-shot: once the holder lets go, an ordinary run
    # gets in again. A guard that refused forever would be just as broken.
    after = _run(lock_path)
    assert after.returncode != REFUSED, (
        "the gate stayed locked after the holder released it")


def test_the_refusal_names_no_host_database_or_role(tmp_path):
    """The refusal is printed by a script whose environment is full of
    connection strings. Entry 11A's rule applies to tooling output too."""
    lock_path = tmp_path / "gate.lock"
    holder = open(lock_path, "w")                      # noqa: SIM115
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(lock_path)
    finally:
        holder.close()

    output = result.stdout + result.stderr
    for forbidden in ("postgres://", "postgresql://", "password", ":test@",
                      NO_CLUSTER):
        assert forbidden not in output, (
            f"the refusal leaked {forbidden!r}: {output!r}")


def test_a_run_that_gets_the_lock_is_not_refused(tmp_path):
    """The guard must be a lock, not a blanket refusal.

    With the lock free the script proceeds — and then fails at the first psql,
    because `PGHOST` deliberately points nowhere. Any exit code other than
    REFUSED proves it got past the guard; that it is non-zero proves it did not
    silently skip the gate either.
    """
    result = _run(tmp_path / "gate.lock")

    assert result.returncode != REFUSED, (
        "the script refused itself with no other run holding the lock")
    assert result.returncode != 0, (
        "the script reported success without a cluster to run against")


def test_the_lock_is_released_when_a_run_is_killed(tmp_path):
    """A crashed or cancelled gate must not wedge the next one.

    Both overlapping runs in this entry were ended with a signal; a lock file
    holding a pid, or a lock released only by a trap, would have left the gate
    unusable afterwards. `flock` is held by an open descriptor, so the kernel
    releases it when the process dies however it dies.

    The holder is a single process that takes the lock itself. `flock FILE CMD`
    would not do: it forks CMD, which inherits the descriptor, so killing the
    flock process alone leaves the lock held by an orphan — which is what the
    first version of this test measured.
    """
    import sys

    lock_path = tmp_path / "gate.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         "f = open(sys.argv[1], 'w')\n"
         "fcntl.flock(f, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\n"
         "time.sleep(300)\n",
         str(lock_path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked", (
            "the holder never reported taking the lock")

        held = subprocess.run(
            ["flock", "-n", "-x", str(lock_path), "true"], check=False)
        assert held.returncode != 0, (
            "the lock was not actually exclusive while the holder was alive")

        holder.kill()
        holder.wait(timeout=30)

        freed = subprocess.run(
            ["flock", "-n", "-x", str(lock_path), "true"], check=False)
        assert freed.returncode == 0, (
            "the lock survived the death of the process holding it — the next "
            "gate run would be refused forever")
    finally:
        if holder.poll() is None:
            holder.kill()


def test_the_gate_declares_a_default_lock_outside_the_repository():
    """The tests above pass their own lock path. Production runs use the
    default, so the default has to exist and has to be a real absolute path —
    two runs that computed different defaults would not exclude each other.
    """
    text = GATE.read_text()
    assert "ONYX_SEC_PROOF_LOCK" in text, "the gate has no lock at all"
    assert "/tmp/onyx_sec_proof.gate.lock" in text, (
        "the default lock path changed; update this test deliberately rather "
        "than letting two runs disagree about which file they lock")
    # The EXECUTABLE line, not the comment above the lock that quotes it.
    lock_at = text.index("ONYX_SEC_PROOF_LOCK")
    trap = re.search(r"^trap cleanup EXIT$", text, re.MULTILINE)
    assert trap is not None, "the gate no longer installs an EXIT trap"
    assert lock_at < trap.start(), (
        "the lock is taken after the EXIT trap is installed, so a refused run "
        "would drop the database belonging to the run that holds the lock")
