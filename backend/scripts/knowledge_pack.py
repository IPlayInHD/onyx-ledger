#!/usr/bin/env python
"""Operator tooling for the governed tax-knowledge pipeline.

Three verbs, in the order an operator uses them:

    validate <manifest.json>              read-only. Writes nothing, ever.
    stage    <manifest.json>              persist drafts + their verdicts
    publish  <version-id>:<spec-hash> …   publish exactly what was validated

`validate` is the one that matters day to day: a content author runs it against
production as often as they like and gets every error at once, with machine
codes, before any knowledge is activated. Hand-entering dozens of INSERTs is how
a tax year comes to half exist.

Not a customer API and not reachable from one. This connects with authoring
authority, which the customer application role does not have.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.session import unit_of_work  # noqa: E402
from app.services.tax_kb.authoring.manifest import (  # noqa: E402
    as_finding,
    load_manifest,
    parse_members,
)
from app.services.tax_kb.authoring.report import PublishabilityReport  # noqa: E402
from app.services.tax_kb.authoring.service import (  # noqa: E402
    KnowledgeAuthoringService,
)
from app.services.tax_kb.authoring.spec import SpecError  # noqa: E402


def _render(report: PublishabilityReport) -> None:
    """Deterministic, machine-readable, and readable by a person."""
    print(json.dumps(report.as_payload(), indent=2, sort_keys=False))
    print(f"\npublishable: {report.publishable}", file=sys.stderr)
    if not report.publishable:
        print("blocking codes: " + ", ".join(report.error_codes()),
              file=sys.stderr)


async def _validate(path: str) -> int:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pack, parse_errors = parse_members(payload)
    async with unit_of_work(actor_type="admin") as session:
        report = await KnowledgeAuthoringService(session).validate_pack(
            pack.rules, pack.reference_data)
    if parse_errors:
        # A member that could not be READ is reported alongside the ones that
        # could, rather than aborting the run — an author needs every error.
        report = PublishabilityReport(
            spec_hash=report.spec_hash,
            spec_schema_version=report.spec_schema_version,
            publication_policy_version=report.publication_policy_version,
            findings=tuple(sorted(
                {*report.findings, *(as_finding(e) for e in parse_errors)})),
            readiness=report.readiness,
            members=report.members)
    _render(report)
    return 0 if report.publishable else 1


async def _stage(path: str) -> int:
    """Persist drafts and their verdicts, printing what `publish` will need.

    Reference data is NOT staged — it has no draft state, because the reference
    tables carry no lifecycle column. Its hash is printed so an operator can
    bind publication to the manifest they validated rather than to whatever the
    file says by then.
    """
    pack = load_manifest(path)
    async with unit_of_work(actor_type="admin") as session:
        service = KnowledgeAuthoringService(session)
        report = await service.validate_pack(pack.rules, pack.reference_data)
        if not report.publishable:
            _render(report)
            return 1
        reference_members = report.members[:len(pack.reference_data)]
        for rd, member in zip(pack.reference_data, reference_members, strict=True):
            print(f"--reference {member.spec_hash}\t{rd.kind} {rd.key}")
        for spec, member in zip(pack.rules, report.members[len(pack.reference_data):],
                                strict=True):
            version_id = await service.stage_draft(spec)
            await service.record_report(version_id, member)
            print(f"{version_id}:{member.spec_hash}\t{spec.rule_code}")
    return 0


async def _publish(publisher: str, targets: list[str], manifest: str | None,
                   reference_hashes: list[str]) -> int:
    """Publish exactly what was validated: rules by id, reference data by hash.

    The hashes come from the operator, not from recomputing the manifest. A file
    edited between validation and publication must be caught, and a value
    derived from the file being published cannot catch it.
    """
    pairs = []
    for target in targets:
        version_id, _, spec_hash = target.partition(":")
        if not spec_hash:
            print(f"expected <version-id>:<spec-hash>, got {target!r}",
                  file=sys.stderr)
            return 2
        pairs.append((uuid.UUID(version_id), spec_hash))

    reference_data = []
    if manifest is not None:
        reference_data = list(load_manifest(manifest).reference_data)
    if len(reference_data) != len(reference_hashes):
        print(f"the manifest carries {len(reference_data)} reference-data "
              f"object(s) but {len(reference_hashes)} --reference hash(es) were "
              "given; publication binds each one explicitly", file=sys.stderr)
        return 2

    async with unit_of_work(actor_type="admin") as session:
        result = await KnowledgeAuthoringService(session).publish(
            uuid.UUID(publisher),
            [version_id for version_id, _ in pairs],
            expected_spec_hashes=[spec_hash for _, spec_hash in pairs],
            reference_data=reference_data,
            expected_reference_hashes=reference_hashes)
    print(json.dumps({
        "pack_hash": result.pack_hash,
        "published": [str(x) for x in result.published_version_ids],
        "superseded": [str(x) for x in result.superseded_version_ids],
        "reference_data": list(result.reference_data_hashes),
        "policy_version": result.policy_version,
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    check = sub.add_parser("validate", help="dry run; writes nothing")
    check.add_argument("manifest")

    stage = sub.add_parser("stage", help="persist drafts and their verdicts")
    stage.add_argument("manifest")

    publish = sub.add_parser("publish", help="publish exactly what was validated")
    publish.add_argument("--publisher", required=True,
                         help="admin id holding tkms.publish")
    publish.add_argument("--manifest",
                         help="manifest supplying the release's reference data")
    publish.add_argument("--reference", action="append", default=[],
                         metavar="SPEC_HASH",
                         help="hash a reference-data object was validated as; "
                              "one per object, in manifest order")
    publish.add_argument("targets", nargs="*", metavar="VERSION_ID:SPEC_HASH")

    args = parser.parse_args()
    try:
        if args.verb == "validate":
            return asyncio.run(_validate(args.manifest))
        if args.verb == "stage":
            return asyncio.run(_stage(args.manifest))
        return asyncio.run(_publish(args.publisher, args.targets,
                                    args.manifest, args.reference))
    except SpecError as exc:
        # A manifest that cannot be read is a coded refusal, not a traceback.
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
