#!/usr/bin/env python
"""BillShield offline extraction evaluation — thin CLI over the typed runner.

All logic lives in `app.services.billshield.evaluation.runner`, inside the
protected mypy scope; this file only parses arguments, builds the one adapter
Slice 1 ships (the deterministic fixture — no network, no secrets), runs the
evaluation, writes the requested outputs, and maps failures to exit codes.

Every location is EXPLICIT: the manifest, the corpus root, the fixture
responses, and each output path. Nothing defaults to scanning the repository
or a home directory.

Usage:
    python scripts/billshield_eval.py \
        --manifest PATH --corpus-root DIR --fixture-responses PATH \
        [--public-out PATH] [--diagnostic-out PATH]

Exit codes: 0 — evaluation completed and the gate verdict is PASS;
1 — evaluation completed and the gate verdict is FAIL (requested reports are
still written before returning); 2 — evaluator/input/configuration error
(closed EvaluationError code on stderr, and argparse usage errors).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.services.billshield.evaluation.manifest import EvaluationError
from app.services.billshield.evaluation.runner import (
    evaluate_corpus,
    write_outputs,
)
from app.services.billshield.extraction.fixture import (
    DeterministicFixtureExtractionProvider,
    FixtureConfigurationError,
    load_fixture_responses,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--corpus-root", required=True, type=Path)
    parser.add_argument(
        "--fixture-responses", required=True, type=Path,
        help="JSON mapping artifact sha256 -> raw provider payload; the "
             "fixture adapter is the only provider this CLI exposes")
    parser.add_argument("--public-out", type=Path, default=None)
    parser.add_argument("--diagnostic-out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        provider = DeterministicFixtureExtractionProvider(
            load_fixture_responses(args.fixture_responses))
        result = asyncio.run(evaluate_corpus(
            manifest_path=args.manifest,
            corpus_root=args.corpus_root,
            provider=provider,
        ))
        write_outputs(
            result,
            public_out=args.public_out,
            diagnostic_out=args.diagnostic_out,
        )
    except EvaluationError as refused:
        print(f"evaluation refused: {refused}", file=sys.stderr)
        return 2
    except FixtureConfigurationError as misconfigured:
        # Expected operator mistakes only — programming defects still raise.
        print(f"fixture configuration error: {misconfigured}", file=sys.stderr)
        return 2
    # Reports were written above; only now does the verdict pick the exit.
    print(f"report_hash={result.report_hash} gate={result.body.gate_verdict}")
    return 0 if result.body.gate_verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
