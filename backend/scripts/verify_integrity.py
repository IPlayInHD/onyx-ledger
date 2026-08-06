#!/usr/bin/env python
"""Manual bounded replay-integrity verification (closure entry 8C).

The supported operator path when the scheduled task is paused, when a batch
must be run immediately, or when an incident calls for a smaller batch than the
configured default.

    PYTHONPATH=. python scripts/verify_integrity.py --batch-size 5
    PYTHONPATH=. python scripts/verify_integrity.py --entity-type scenario

It calls the SAME `IntegrityScheduler` the Celery task calls, which claims
through `ioe.claim_integrity_targets` and verifies through
`IntegrityVerificationService`. It therefore cannot bypass the privileged claim
interface, cannot skip the verifier, and cannot reach a tenant's rows outside
the RLS context the verification establishes for itself. There is deliberately
no flag that would let it do any of those things.

It is not an HTTP endpoint: verification is an operational action, not a
product surface, and exposing it would put an engine run behind a request.

Output is counts and enumerated reason codes only — nothing identifying, so the
output is safe to paste into an incident channel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.services.ioe.replay.scheduler import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_BATCH_SIZE,
    SUPPORTED_TARGET_TYPES,
    IntegrityScheduler,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_integrity",
        description="Run one bounded replay-integrity verification cycle.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=(
            f"Records for the WHOLE cycle across all target types "
            f"(default {DEFAULT_BATCH_SIZE}, hard cap {MAX_BATCH_SIZE})."
        ),
    )
    parser.add_argument(
        "--entity-type", action="append", choices=list(SUPPORTED_TARGET_TYPES),
        help=(
            "Restrict to one target type; repeatable. "
            f"Default: {', '.join(SUPPORTED_TARGET_TYPES)}."
        ),
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-record ceiling so one pathological record cannot hold the run.",
    )
    parser.add_argument(
        "--worker-id", default=None,
        help="Claim identity. Defaults to host:pid, as the Celery worker uses.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, object]:
    scheduler = IntegrityScheduler(worker_id=args.worker_id)
    report = await scheduler.run_cycle(
        batch_size=args.batch_size,
        timeout_seconds=args.timeout_seconds,
        entity_types=tuple(args.entity_type or SUPPORTED_TARGET_TYPES),
    )
    out: dict[str, object] = dict(report.as_metrics())
    out["reason_codes"] = sorted(set(report.reason_codes))
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    metrics = asyncio.run(run(args))
    json.dump(metrics, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    # A mismatch is the one outcome an operator must not miss in a pipeline.
    return 2 if metrics["integrity_batch_mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
