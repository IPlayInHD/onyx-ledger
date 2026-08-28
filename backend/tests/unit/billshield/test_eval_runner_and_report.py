"""The typed runner, the report envelope, and the output policy.

Tmp-path corpora are built per test with real digests; the committed
synthetic corpus under tests/fixtures/billshield backs the golden and the
leak checks. Nothing here touches a database or a network.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.billshield.evaluation.manifest import (
    EvaluationError,
    EvaluationLimits,
)
from app.services.billshield.evaluation.report import verify_envelope
from app.services.billshield.evaluation.runner import (
    evaluate_corpus,
    serialize_payload,
    write_outputs,
)
from app.services.billshield.extraction.codes import (
    DiagnosticCode,
    EvaluationErrorCode,
)
from app.services.billshield.extraction.fixture import (
    DeterministicFixtureExtractionProvider,
)

BACKEND = Path(__file__).resolve().parents[3]
FIXTURES = BACKEND / "tests" / "fixtures" / "billshield"
GOLDEN = BACKEND / "tests" / "golden" / "billshield_eval_synthetic_v1.json"


def _committed_provider() -> DeterministicFixtureExtractionProvider:
    responses = json.loads(
        (FIXTURES / "fixture_responses.json").read_text(encoding="utf-8"))
    return DeterministicFixtureExtractionProvider(responses)


async def _run_committed():
    return await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus",
        provider=_committed_provider())


# ---------------------------------------------------------------------------
# Tmp corpora
# ---------------------------------------------------------------------------
_LABEL = {
    "label_schema_version": "1.0.0",
    "expected_outcome": "success",
    "fields": {
        "bill_issuer_name": {"state": "present", "value": "Tmp Issuer Co"},
        "service_category": {"state": "present", "value": "INTERNET"},
        "statement_date": {"state": "absent_on_document"},
        "billing_period": {"state": "absent_on_document"},
        "amount_due": {"state": "present", "value": "42.00"},
        "previous_balance": {"state": "absent_on_document"},
        "payments_applied": {"state": "absent_on_document"},
        "subtotal_before_tax": {"state": "absent_on_document"},
        "total_tax": {"state": "absent_on_document"},
    },
    "charges": [],
    "promotions": [],
}

_RESPONSE = {
    "schema_version": "1.0.0",
    "extraction": {
        "currency": "CAD",
        "bill_issuer_name": {"state": "no_candidate"},
        "service_category": {"state": "no_candidate"},
        "statement_date": {"state": "no_candidate"},
        "billing_period": {"state": "no_candidate"},
        "amount_due": {
            "state": "present", "value": "42.00", "confidence": "0.9",
            "evidence": [{"page": 1, "x0": "0.1", "y0": "0.2",
                          "x1": "0.5", "y1": "0.25"}]},
        "previous_balance": {"state": "no_candidate"},
        "payments_applied": {"state": "no_candidate"},
        "subtotal_before_tax": {"state": "no_candidate"},
        "total_tax": {"state": "no_candidate"},
        "charges": [],
        "promotions": [],
    },
}


def _tmp_corpus(
    tmp_path: Path, *, origin: str = "synthetic",
    artifact: bytes = b"tmp synthetic artifact bytes\n",
    label: dict | None = None,
    manifest_overrides: dict | None = None,
) -> tuple[Path, Path, DeterministicFixtureExtractionProvider]:
    root = tmp_path / "corpus"
    (root / "artifacts").mkdir(parents=True)
    (root / "labels").mkdir(parents=True)
    label_bytes = json.dumps(label if label is not None else _LABEL).encode()
    artifact_sha = hashlib.sha256(artifact).hexdigest()
    approvals = ("not_applicable" if origin == "synthetic" else "approved")
    sample = {
        "sample_id": "s-0123456789ab",
        "origin": origin,
        "artifact_sha256": artifact_sha,
        "artifact_format": "pdf_native",
        "page_count": 1,
        "language_layout": "en",
        "label_sha256": hashlib.sha256(label_bytes).hexdigest(),
        "consent_state": approvals,
        "redaction_state": approvals,
    }
    sample.update(manifest_overrides or {})
    (root / "artifacts" / "s-0123456789ab.pdf").write_bytes(artifact)
    (root / "labels" / "s-0123456789ab.json").write_bytes(label_bytes)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "manifest_schema_version": "1.0.0",
        "label_schema_version": "1.0.0",
        "corpus_id": "c-0123456789ab",
        "samples": [sample],
    }))
    provider = DeterministicFixtureExtractionProvider({artifact_sha: _RESPONSE})
    return manifest_path, root, provider


# ---------------------------------------------------------------------------
# Golden and determinism
# ---------------------------------------------------------------------------
async def test_the_synthetic_evaluation_reproduces_the_golden_byte_for_byte():
    result = await _run_committed()
    rendered = serialize_payload(
        {"public": result.public, "diagnostics": result.diagnostics})
    assert rendered == GOLDEN.read_bytes(), (
        "the synthetic evaluation no longer reproduces its golden; if a "
        "policy or metric deliberately changed, regenerating the golden is a "
        "reviewed act, not a reflex")


async def test_two_runs_produce_identical_bytes():
    first = await _run_committed()
    second = await _run_committed()
    assert serialize_payload(first.public) == serialize_payload(second.public)
    assert serialize_payload(first.diagnostics) == serialize_payload(
        second.diagnostics)


def _pass_provider() -> DeterministicFixtureExtractionProvider:
    """The STATIC all-correct response set, committed beside the corpus.

    Authored by hand against the labels and committed as data — the test
    never derives a response from a label, so nothing here is circular.
    """
    responses = json.loads(
        (FIXTURES / "fixture_responses_pass.json").read_text(encoding="utf-8"))
    return DeterministicFixtureExtractionProvider(responses)


async def test_an_all_correct_corpus_passes_the_gate_end_to_end():
    """The complete real pipeline — manifest, bounded reads, fixture
    provider, strict parser, metrics, gate, envelope — observed producing
    PASS, so the gate's positive direction is proven at the same level as
    its refusals."""
    from app.services.billshield.evaluation.metrics import (
        CRITICAL_CHARGE_METRICS,
        CRITICAL_SCALAR_FIELDS,
    )

    first = await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus", provider=_pass_provider())

    critical = [f"field.{name}.accuracy" for name in CRITICAL_SCALAR_FIELDS]
    critical.extend(CRITICAL_CHARGE_METRICS)
    for name in critical:
        payload = first.body.metrics[name]
        assert payload["status"] == "MEASURED" and payload["denominator"] > 0, (
            f"critical metric {name} is not measurable on the PASS corpus")
    assert first.body.gate_verdict == "PASS"
    assert first.body.gate_failures == ()
    assert verify_envelope(first.public)

    second = await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus", provider=_pass_provider())
    assert serialize_payload(first.public) == serialize_payload(second.public)
    assert serialize_payload(first.diagnostics) == serialize_payload(
        second.diagnostics)


def test_report_bytes_are_stable_across_hash_seeds(tmp_path):
    """The whole pipeline under PYTHONHASHSEED 0/1/42 — same bytes, same
    report hash. The child writes to a file; stdout carries only a digest."""
    import os

    script = tmp_path / "probe.py"
    script.write_text(
        "import asyncio, hashlib, json, sys\n"
        "from pathlib import Path\n"
        "from app.services.billshield.evaluation.runner import "
        "evaluate_corpus, serialize_payload\n"
        "from app.services.billshield.extraction.fixture import "
        "DeterministicFixtureExtractionProvider\n"
        f"fixtures = Path({str(FIXTURES)!r})\n"
        "responses = json.loads((fixtures / 'fixture_responses.json')"
        ".read_text(encoding='utf-8'))\n"
        "result = asyncio.run(evaluate_corpus(\n"
        "    manifest_path=fixtures / 'manifest.json',\n"
        "    corpus_root=fixtures / 'corpus',\n"
        "    provider=DeterministicFixtureExtractionProvider(responses)))\n"
        "rendered = serialize_payload("
        "{'public': result.public, 'diagnostics': result.diagnostics})\n"
        "print(hashlib.sha256(rendered).hexdigest())\n")
    digests = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed,
               "PYTHONPATH": str(BACKEND)}
        run = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True,
            env=env, cwd=BACKEND, timeout=120)
        assert run.returncode == 0, run.stderr
        digests.add(run.stdout.strip())
    assert len(digests) == 1, f"report bytes moved across hash seeds: {digests}"


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
async def test_the_report_hash_verifies_without_self_reference():
    result = await _run_committed()
    assert verify_envelope(result.public)
    assert "report_hash" not in result.public["body"], (
        "the hash must not live inside what it hashes")
    tampered = json.loads(serialize_payload(result.public).decode())
    tampered["body"]["gate_verdict"] = "PASS"
    assert not verify_envelope(tampered), "a tampered body kept its identity"


async def test_provenance_changes_move_the_report_hash():
    baseline = (await _run_committed()).report_hash
    changed_model = _committed_provider()
    changed_model.model_version = "fixture-2.0.0"
    with_model = await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus", provider=changed_model)
    assert with_model.report_hash != baseline
    changed_prompt = _committed_provider()
    changed_prompt.prompt_version = "prompt-7"
    with_prompt = await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus", provider=changed_prompt)
    assert with_prompt.report_hash not in (baseline, with_model.report_hash)


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------
async def test_the_public_report_contains_no_sample_ids_issuers_or_values():
    result = await _run_committed()
    rendered = serialize_payload(result.public).decode()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for sample in manifest["samples"]:
        assert sample["sample_id"] not in rendered
    for private in ("Maple Télécom", "Rivière TV", "Northern Internet",
                    "Forfait mobile 5G", "Sports add-on", "SYNTHETIC",
                    "91.25", "x0"):
        assert private not in rendered, private
    body = result.public["body"]
    assert "counts_by_issuer" not in body, (
        "no per-issuer public breakdown until a governed resolver exists")
    assert set(body["counts_by_category"]) <= {
        "MOBILE", "INTERNET", "TV", "HOME_PHONE", "BUNDLE", "STREAMING",
        "OTHER_SUBSCRIPTION", "refusal_expected", "unlabeled"}


async def test_diagnostics_carry_only_ids_subjects_and_codes():
    result = await _run_committed()
    codes = {code.value for code in DiagnosticCode}
    for finding in result.diagnostics["findings"]:
        assert set(finding) == {"sample_id", "subject", "code"}
        assert finding["code"] in codes
    rendered = serialize_payload(result.diagnostics).decode()
    for private in ("Maple", "Forfait", "91.25", "-7.80", "2026-12-31"):
        assert private not in rendered, private


async def test_sentinel_values_never_reach_outputs_or_errors(tmp_path):
    sentinel = "SENTINEL-CUSTOMER-4111111111111111"
    label = json.loads(json.dumps(_LABEL))
    label["fields"]["bill_issuer_name"]["value"] = sentinel
    label["charges"] = [
        {"label": sentinel, "amount": "9.99", "kind": "ONE_TIME",
         "cadence": None}]
    manifest_path, root, provider = _tmp_corpus(
        tmp_path, artifact=f"artifact {sentinel}\n".encode(), label=label)
    result = await evaluate_corpus(
        manifest_path=manifest_path, corpus_root=root, provider=provider)
    for payload in (result.public, result.diagnostics):
        assert sentinel not in serialize_payload(payload).decode()


# ---------------------------------------------------------------------------
# Runner refusals
# ---------------------------------------------------------------------------
async def test_hash_mismatch_and_missing_files_fail_closed(tmp_path):
    manifest_path, root, provider = _tmp_corpus(
        tmp_path, manifest_overrides={"artifact_sha256": "f" * 64})
    with pytest.raises(EvaluationError) as caught:
        await evaluate_corpus(
            manifest_path=manifest_path, corpus_root=root, provider=provider)
    assert caught.value.code in (
        EvaluationErrorCode.ARTIFACT_HASH_MISMATCH,
        EvaluationErrorCode.MISSING_ARTIFACT)

    manifest_path, root, provider = _tmp_corpus(tmp_path / "second")
    (root / "labels" / "s-0123456789ab.json").unlink()
    with pytest.raises(EvaluationError) as caught:
        await evaluate_corpus(
            manifest_path=manifest_path, corpus_root=root, provider=provider)
    assert caught.value.code is EvaluationErrorCode.MISSING_LABEL


async def test_byte_limits_refuse_before_any_content_is_read(
        tmp_path, monkeypatch):
    """`os.fstat` on the open descriptor rejects an oversized file before a
    single byte is read: the wrapped handle explodes on any read call."""
    manifest_path, root, provider = _tmp_corpus(
        tmp_path, artifact=b"x" * 64)
    tight = EvaluationLimits(
        version="test", max_artifact_bytes=16, max_artifact_pages=50,
        max_label_bytes=2 * 1024 * 1024, max_manifest_samples=500)

    real_open = pathlib.Path.open

    class _NoReadHandle:
        def __init__(self, handle):
            self._handle = handle

        def read(self, *args: object) -> bytes:
            raise AssertionError(
                "content was read despite the fstat limit refusing it")

        def fileno(self) -> int:
            return self._handle.fileno()

        def __enter__(self) -> _NoReadHandle:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

    def guarded_open(self: pathlib.Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self.suffix == ".pdf":
            return _NoReadHandle(handle)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", guarded_open)
    with pytest.raises(EvaluationError) as caught:
        await evaluate_corpus(
            manifest_path=manifest_path, corpus_root=root,
            provider=provider, limits=tight)
    assert caught.value.code is EvaluationErrorCode.ARTIFACT_TOO_LARGE


def test_a_file_that_grows_after_fstat_is_still_rejected(tmp_path, monkeypatch):
    """The TOCTOU window is closed by the CAPPED read: `os.fstat` is made to
    lie that the file is tiny, the file actually holds limit+5 bytes, and the
    single read request must ask for exactly limit+1 — never unbounded — and
    the oversized result must still come back as the closed error."""
    import os

    from app.services.billshield.evaluation.runner import _read_bounded

    limit = 16
    target = tmp_path / "grow.bin"
    target.write_bytes(b"g" * (limit + 5))

    requested: list[int] = []
    real_open = pathlib.Path.open

    class _Recording:
        def __init__(self, handle):
            self._handle = handle

        def read(self, n: int = -1) -> bytes:
            requested.append(n)
            return self._handle.read(n)

        def fileno(self) -> int:
            return self._handle.fileno()

        def __enter__(self) -> _Recording:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

    def recording_open(self: pathlib.Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return _Recording(handle) if self == target else handle

    class _LyingStat:
        st_size = 1  # "within the limit" — the file is not

    monkeypatch.setattr(pathlib.Path, "open", recording_open)
    monkeypatch.setattr(os, "fstat", lambda fd: _LyingStat())

    with pytest.raises(EvaluationError) as caught:
        _read_bounded(
            target, limit=limit,
            missing=EvaluationErrorCode.MISSING_ARTIFACT,
            too_large=EvaluationErrorCode.ARTIFACT_TOO_LARGE,
            sample_id="s-0123456789ab")
    assert caught.value.code is EvaluationErrorCode.ARTIFACT_TOO_LARGE
    assert requested == [limit + 1], (
        f"the read was not capped at limit+1: {requested}")


async def test_unapproved_states_refuse_before_any_file_is_opened(monkeypatch):
    """The approval gate runs before path derivation, before resolution, and
    before any open: with `Path.open` rigged to explode, every non-APPROVED
    customer-derived state still refuses with its own closed code."""
    from app.services.billshield.evaluation.manifest import (
        EVALUATION_LIMITS_V1,
        ApprovalState,
        CorpusSample,
        LanguageLayout,
        SampleOrigin,
    )
    from app.services.billshield.evaluation.runner import _load_sample
    from app.services.billshield.extraction.ports import ArtifactFormat

    def boom(self: pathlib.Path, *args: object, **kwargs: object):
        raise AssertionError(f"a file was opened for an unapproved sample: {self}")

    monkeypatch.setattr(pathlib.Path, "open", boom)

    cases = [
        (ApprovalState.PENDING, ApprovalState.APPROVED,
         EvaluationErrorCode.SAMPLE_NOT_CONSENTED),
        (ApprovalState.REVOKED, ApprovalState.APPROVED,
         EvaluationErrorCode.SAMPLE_NOT_CONSENTED),
        (ApprovalState.NOT_APPLICABLE, ApprovalState.APPROVED,
         EvaluationErrorCode.SAMPLE_NOT_CONSENTED),
        (ApprovalState.APPROVED, ApprovalState.PENDING,
         EvaluationErrorCode.SAMPLE_NOT_REDACTION_APPROVED),
        (ApprovalState.APPROVED, ApprovalState.REVOKED,
         EvaluationErrorCode.SAMPLE_NOT_REDACTION_APPROVED),
        (ApprovalState.APPROVED, ApprovalState.NOT_APPLICABLE,
         EvaluationErrorCode.SAMPLE_NOT_REDACTION_APPROVED),
    ]
    for consent, redaction, expected in cases:
        sample = CorpusSample(
            sample_id="s-0123456789ab", origin=SampleOrigin.CUSTOMER_DERIVED,
            artifact_sha256="a" * 64, artifact_format=ArtifactFormat.PDF_NATIVE,
            page_count=1, language_layout=LanguageLayout.EN,
            label_sha256="b" * 64, consent_state=consent,
            redaction_state=redaction)
        with pytest.raises(EvaluationError) as caught:
            _load_sample(sample, pathlib.Path("/nonexistent-root"),
                         limits=EVALUATION_LIMITS_V1)
        assert caught.value.code is expected, (consent, redaction)


async def test_unapproved_customer_samples_are_refused(tmp_path):
    manifest_path, root, provider = _tmp_corpus(
        tmp_path, origin="customer_derived",
        manifest_overrides={"consent_state": "pending"})
    with pytest.raises(EvaluationError) as caught:
        await evaluate_corpus(
            manifest_path=manifest_path, corpus_root=root, provider=provider)
    assert caught.value.code is EvaluationErrorCode.SAMPLE_NOT_CONSENTED


async def test_evidence_beyond_the_manifest_page_count_is_invalid_output(
        tmp_path):
    """The artifact handed to the parser carries the manifest's page count,
    so a locator past it is caught even when the payload is well-formed."""
    response = json.loads(json.dumps(_RESPONSE))
    response["extraction"]["amount_due"]["evidence"][0]["page"] = 3
    manifest_path, root, provider = _tmp_corpus(tmp_path)
    provider = DeterministicFixtureExtractionProvider(
        {next(iter(provider._responses)): response})
    result = await evaluate_corpus(
        manifest_path=manifest_path, corpus_root=root, provider=provider)
    assert result.body.counts["invalid_output"] == 1


# ---------------------------------------------------------------------------
# Output policy
# ---------------------------------------------------------------------------
async def test_customer_diagnostics_cannot_be_written_inside_a_repository(
        tmp_path):
    manifest_path, root, provider = _tmp_corpus(
        tmp_path, origin="customer_derived")
    result = await evaluate_corpus(
        manifest_path=manifest_path, corpus_root=root, provider=provider)

    repo = tmp_path / "somerepo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(EvaluationError) as caught:
        write_outputs(result, public_out=None,
                      diagnostic_out=repo / "out" / "diag.json")
    assert caught.value.code is (
        EvaluationErrorCode.DIAGNOSTIC_PATH_IN_REPOSITORY)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere/else\n")
    with pytest.raises(EvaluationError):
        write_outputs(result, public_out=None,
                      diagnostic_out=worktree / "diag.json")

    outside = tmp_path / "external" / "diag.json"
    write_outputs(result, public_out=None, diagnostic_out=outside)
    assert outside.exists()


async def test_synthetic_diagnostics_may_back_the_committed_golden(tmp_path):
    manifest_path, root, provider = _tmp_corpus(tmp_path)  # synthetic
    result = await evaluate_corpus(
        manifest_path=manifest_path, corpus_root=root, provider=provider)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    target = repo / "diag.json"
    write_outputs(result, public_out=repo / "public.json",
                  diagnostic_out=target)
    assert target.exists()
    assert verify_envelope(json.loads((repo / "public.json").read_text()))


def test_the_cli_is_thin_offline_and_reports_the_failing_golden(tmp_path):
    """The CLI in a subprocess with sockets disabled, over the deliberately
    FAILING golden corpus: no network attempt, exit 1 (evaluation completed,
    gate FAIL), and the requested reports are still written — matching the
    committed golden byte-for-byte."""
    wrapper = tmp_path / "wrapper.py"
    public_out = tmp_path / "public.json"
    diag_out = tmp_path / "diag.json"
    wrapper.write_text(
        "import socket, sys, runpy\n"
        "class _Refuse(socket.socket):\n"
        "    def connect(self, *a): raise AssertionError('network attempt')\n"
        "    def connect_ex(self, *a): raise AssertionError('network attempt')\n"
        "socket.socket = _Refuse\n"
        f"sys.argv = ['billshield_eval.py',\n"
        f"    '--manifest', {str(FIXTURES / 'manifest.json')!r},\n"
        f"    '--corpus-root', {str(FIXTURES / 'corpus')!r},\n"
        f"    '--fixture-responses', {str(FIXTURES / 'fixture_responses.json')!r},\n"
        f"    '--public-out', {str(public_out)!r},\n"
        f"    '--diagnostic-out', {str(diag_out)!r}]\n"
        f"runpy.run_path({str(BACKEND / 'scripts' / 'billshield_eval.py')!r},"
        " run_name='__main__')\n")
    import os

    run = subprocess.run(
        [sys.executable, str(wrapper)], capture_output=True, text=True,
        cwd=BACKEND, env={**os.environ, "PYTHONPATH": str(BACKEND)},
        timeout=120)
    assert run.returncode == 1, (run.returncode, run.stderr)
    assert "gate=FAIL" in run.stdout
    assert diag_out.exists(), "a FAIL evaluation must still write its reports"
    public = json.loads(public_out.read_text())
    assert verify_envelope(public)
    combined = serialize_payload({
        "public": public,
        "diagnostics": json.loads(diag_out.read_text()),
    })
    assert combined == GOLDEN.read_bytes(), (
        "the CLI's FAIL run no longer reproduces the committed golden")


def test_the_cli_exit_contract_covers_pass_fail_and_error(tmp_path):
    """0 = completed + PASS; 1 = completed + FAIL; 2 = evaluator error.
    Driven through main() in-process so the codes are asserted exactly."""
    from scripts.billshield_eval import main

    common = ["--manifest", str(FIXTURES / "manifest.json"),
              "--corpus-root", str(FIXTURES / "corpus")]

    pass_public = tmp_path / "pass_public.json"
    assert main([*common,
                 "--fixture-responses",
                 str(FIXTURES / "fixture_responses_pass.json"),
                 "--public-out", str(pass_public)]) == 0
    assert verify_envelope(json.loads(pass_public.read_text()))

    fail_public = tmp_path / "fail_public.json"
    assert main([*common,
                 "--fixture-responses",
                 str(FIXTURES / "fixture_responses.json"),
                 "--public-out", str(fail_public)]) == 1
    assert fail_public.exists(), "FAIL still writes its requested reports"

    assert main(["--manifest", str(tmp_path / "no-such-manifest.json"),
                 "--corpus-root", str(FIXTURES / "corpus"),
                 "--fixture-responses",
                 str(FIXTURES / "fixture_responses.json")]) == 2

    with pytest.raises(SystemExit) as usage:
        main(["--manifest-misspelt", "x"])
    assert usage.value.code == 2, "argparse usage errors share exit 2"


async def test_a_wrong_label_version_is_rejected_before_any_sample_is_opened(
        tmp_path):
    """The manifest's label declaration is judged during manifest parsing —
    with the sample files DELETED, the failure must still be the version
    refusal, never a missing-file error, proving nothing was opened."""
    manifest_path, root, provider = _tmp_corpus(tmp_path)
    tampered = json.loads(manifest_path.read_text())
    tampered["label_schema_version"] = "999.0.0"
    manifest_path.write_text(json.dumps(tampered))
    (root / "artifacts" / "s-0123456789ab.pdf").unlink()
    (root / "labels" / "s-0123456789ab.json").unlink()
    with pytest.raises(EvaluationError) as caught:
        await evaluate_corpus(
            manifest_path=manifest_path, corpus_root=root, provider=provider)
    assert caught.value.code is EvaluationErrorCode.UNSUPPORTED_LABEL_VERSION


async def test_report_provenance_carries_the_verified_manifest_declaration():
    result = await evaluate_corpus(
        manifest_path=FIXTURES / "manifest.json",
        corpus_root=FIXTURES / "corpus", provider=_pass_provider())
    declared = json.loads(
        (FIXTURES / "manifest.json").read_text())["label_schema_version"]
    assert result.body.provenance.label_schema_version == declared, (
        "provenance must report what the manifest declared and the parser "
        "verified — not an independently substituted constant")


def test_fixture_configuration_errors_all_return_exit_two(tmp_path):
    """Missing, malformed, invalid-UTF-8, non-object, and missing-artifact
    fixture configurations are operator errors: exit 2, no traceback escape
    (an escape would raise out of main() and fail this test), and no fixture
    content echoed."""
    from scripts.billshield_eval import main

    common = ["--manifest", str(FIXTURES / "manifest.json"),
              "--corpus-root", str(FIXTURES / "corpus")]

    missing = tmp_path / "nope.json"
    assert main([*common, "--fixture-responses", str(missing)]) == 2

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    assert main([*common, "--fixture-responses", str(malformed)]) == 2

    bad_utf8 = tmp_path / "bad_utf8.json"
    bad_utf8.write_bytes(b'{"a": "\xff\xfe"}')
    assert main([*common, "--fixture-responses", str(bad_utf8)]) == 2

    non_object = tmp_path / "list.json"
    non_object.write_text("[1, 2, 3]", encoding="utf-8")
    assert main([*common, "--fixture-responses", str(non_object)]) == 2

    # a mapping that lacks a manifested artifact's response
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert main([*common, "--fixture-responses", str(empty)]) == 2


def test_fixture_loader_messages_never_echo_fixture_content(tmp_path):
    from app.services.billshield.extraction.fixture import (
        FixtureConfigurationError,
        load_fixture_responses,
    )

    sentinel = "SENTINEL-FIXTURE-CONTENT-4111111111111111"
    for name, payload in (("bad.json", f'{{"x": "{sentinel}"'),
                          ("list.json", f'["{sentinel}"]')):
        target = tmp_path / name
        target.write_text(payload, encoding="utf-8")
        with pytest.raises(FixtureConfigurationError) as caught:
            load_fixture_responses(target)
        assert sentinel not in f"{caught.value!r} {caught.value!s}"
