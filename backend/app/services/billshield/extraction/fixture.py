"""The deterministic fixture adapter — the only provider Slice 1 ships.

Same shape as the pipeline's other deterministic test implementations
(`FixedClock`, `SequentialIdGen` in `tkms/domain/ports.py`): given the same
artifact it returns the same result, always, with no network, no environment,
no secret, no clock. Responses are keyed by the artifact's byte digest —
content addressing, never a filename — and every canned payload passes
through the SAME strict parser a real adapter's output will, so the fixture
cannot hand the evaluator anything a real provider could not.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from app.services.billshield.extraction.contract import (
    BillExtractionRefusal,
    BillExtractionV1,
)
from app.services.billshield.extraction.parser import parse_extraction_result
from app.services.billshield.extraction.ports import ValidatedBillArtifact

#: Tooling bound for one fixture-response file — a resource guard for the
#: offline evaluator's own configuration, deliberately separate from the
#: artifact/label limits so no evaluation error code is misused for it.
_MAX_FIXTURE_RESPONSE_BYTES = 25 * 1024 * 1024


class FixtureConfigurationError(KeyError):
    """The fixture setup itself is wrong — an OPERATOR/TEST configuration
    failure, deliberately not a domain refusal: a real adapter never "has no
    response", and mapping any of these to UNREADABLE would let a miswired
    run measure a refusal that never happened. Messages carry fixed wording
    and at most a path or digest — never fixture or provider content.
    """


def load_fixture_responses(path: Path) -> dict[str, object]:
    """Read a fixture-response file inside the configuration-error boundary.

    Missing, unreadable, oversized, invalid-UTF-8, malformed-JSON, and
    non-object configurations all surface as FixtureConfigurationError so a
    caller (the CLI) can map every expected operator mistake to one exit
    code without catching programming defects.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_FIXTURE_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise FixtureConfigurationError(
            f"fixture responses are not readable at {path}") from exc
    if len(data) > _MAX_FIXTURE_RESPONSE_BYTES:
        raise FixtureConfigurationError(
            f"fixture responses at {path} exceed the tooling bound")
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FixtureConfigurationError(
            f"fixture responses at {path} are not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FixtureConfigurationError(
            f"fixture responses at {path} must be a JSON object mapping "
            "artifact digests to payloads")
    return payload


class DeterministicFixtureExtractionProvider:
    """artifact digest → canned raw payload → the one strict parser."""

    adapter_code = "fixture"
    model_version = "fixture-1.0.0"
    prompt_version: str | None = None

    def __init__(self, responses: Mapping[str, object]):
        self._responses = dict(responses)

    async def extract(
        self, artifact: ValidatedBillArtifact
    ) -> BillExtractionV1 | BillExtractionRefusal:
        try:
            payload = self._responses[artifact.artifact_sha256]
        except KeyError:
            raise FixtureConfigurationError(
                f"no fixture response for artifact {artifact.artifact_sha256}"
            ) from None
        return parse_extraction_result(payload, page_count=artifact.page_count)


__all__ = ["DeterministicFixtureExtractionProvider", "FixtureConfigurationError"]
