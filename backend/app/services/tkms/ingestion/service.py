"""ImportService — the ingestion plane: create/track jobs, store raw documents
(checksum + dedupe), parse, and stage the canonical extracted rules.

Each stage persists its output so the pipeline is resumable and idempotent:
re-running `parse`/`extract`/... resumes from the last good stage rather than
restarting. The service never talks to Celery directly — workers wrap these
methods — so the whole pipeline is unit/integration testable synchronously.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Conflict, NotFound, ValidationError
from app.database.models import ExtractedRule as ExtractedRuleRow
from app.database.models import ImportJob, ParseResult, RawDocument
from app.integrations.storage import LocalObjectStorage, get_object_storage
from app.services.tkms.parsers import build_default_registry
from app.services.tkms.parsers.base import BaseParser
from app.services.tkms.parsers.registry import ParserRegistry


class ImportService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: LocalObjectStorage | None = None,
        registry: ParserRegistry | None = None,
    ) -> None:
        self.s = session
        self.settings = get_settings()
        self.storage = storage or get_object_storage()
        self.registry = registry or build_default_registry()

    # ---- create + store ------------------------------------------------------
    async def create_job(
        self,
        *,
        source_org: str,
        fmt: str,
        operator_admin_id: uuid.UUID | None = None,
        jurisdiction_code: str | None = None,
        province_code: str | None = None,
        tax_year: int | None = None,
        source_url: str | None = None,
        document_version: str | None = None,
        parser_name: str | None = None,
        parser_version: str | None = None,
    ) -> ImportJob:
        self.registry.require_format(fmt)
        job = ImportJob(
            source_org=source_org,
            format=fmt,
            operator_admin_id=operator_admin_id,
            jurisdiction_code=jurisdiction_code,
            province_code=province_code,
            tax_year=tax_year,
            source_url=source_url,
            document_version=document_version,
            parser_name=parser_name,
            parser_version=parser_version,
            status="received",
        )
        self.s.add(job)
        await self.s.flush()
        return job

    async def store_raw(
        self, job_id: uuid.UUID, raw: bytes, *, mime_type: str | None = None
    ) -> RawDocument:
        """Store the source bytes, compute the checksum, and dedupe by it.

        Idempotency key: at most one import per raw-document checksum (enforced
        here for a friendly error and by the DB partial-unique index)."""
        job = await self._get_job(job_id)
        checksum = hashlib.sha256(raw).hexdigest()

        dupe = await self.s.scalar(
            select(ImportJob).where(
                ImportJob.checksum == checksum, ImportJob.id != job_id
            )
        )
        if dupe is not None:
            raise Conflict(
                f"Duplicate import: checksum already ingested by job {dupe.id}"
            )

        bucket = self.settings.s3_bucket_legislation
        key = f"{job_id}/raw/{uuid.uuid4()}"
        self.storage.put(bucket, key, raw)

        doc = RawDocument(
            import_job_id=job_id,
            storage_bucket=bucket,
            object_key=key,
            content_hash=checksum,
            mime_type=mime_type,
            byte_size=len(raw),
        )
        self.s.add(doc)
        job.checksum = checksum
        job.status = "stored"
        job.processing_started_at = datetime.now(tz=UTC)
        await self.s.flush()
        return doc

    # ---- parse (text extraction) --------------------------------------------
    async def parse(self, job_id: uuid.UUID) -> ParseResult:
        """Resolve a parser, extract text from the raw bytes, persist it."""
        job = await self._get_job(job_id)
        existing = await self._latest_succeeded_parse(job_id)
        if existing is not None:
            return existing

        raw_doc = await self.s.scalar(
            select(RawDocument)
            .where(RawDocument.import_job_id == job_id)
            .order_by(RawDocument.created_at.desc())
        )
        if raw_doc is None:
            raise ValidationError("Cannot parse: no stored raw document for this job")

        parser = self._resolve_parser(job)
        job.status = "parsing"
        await self.s.flush()

        pr = ParseResult(
            import_job_id=job_id,
            parser_name=parser.name,
            parser_version=parser.version,
            status="pending",
        )
        self.s.add(pr)
        await self.s.flush()

        try:
            raw = self.storage.get(raw_doc.storage_bucket, raw_doc.object_key)
            text = parser.extract_text(raw)
            text_key = f"{job_id}/text/{pr.id}"
            self.storage.put(self.settings.s3_bucket_legislation, text_key, text.encode("utf-8"))
            pr.text_object_key = text_key
            pr.status = "succeeded"
            job.status = "parsed"
            job.parser_name = parser.name
            job.parser_version = parser.version
        except Exception as e:  # parser/text failure is recorded, not swallowed
            pr.status = "failed"
            pr.error = str(e)
            job.status = "failed"
            job.error = f"parse: {e}"
            await self.s.flush()
            raise
        await self.s.flush()
        return pr

    # ---- extract (stage canonical rules) ------------------------------------
    async def extract(self, job_id: uuid.UUID) -> list[ExtractedRuleRow]:
        """Run the parser's structured extraction and stage immutable rows."""
        job = await self._get_job(job_id)
        pr = await self._latest_succeeded_parse(job_id)
        if pr is None:
            raise ValidationError("Cannot extract before a successful parse")

        already = list(
            await self.s.scalars(
                select(ExtractedRuleRow)
                .where(ExtractedRuleRow.parse_result_id == pr.id)
                .order_by(ExtractedRuleRow.ordinal)
            )
        )
        if already:
            return already

        parser = self._resolve_parser(job)
        if pr.text_object_key is None:
            # The object store returns empty bytes for a missing key, so passing
            # None through would have staged a zero-rule extraction as a SUCCESS
            # instead of reporting that the parsed text was never persisted.
            raise ValidationError("Parse result has no stored text object to extract from")
        text_bytes = self.storage.get(self.settings.s3_bucket_legislation, pr.text_object_key)
        job.status = "parsing"
        try:
            ruleset = parser.extract_rules(text_bytes.decode("utf-8"))
        except Exception as e:
            pr.status = "failed"
            pr.error = str(e)
            job.status = "failed"
            job.error = f"extract: {e}"
            await self.s.flush()
            raise

        rows: list[ExtractedRuleRow] = []
        for ordinal, rule in enumerate(ruleset.rules):
            row = ExtractedRuleRow(
                parse_result_id=pr.id,
                import_job_id=job_id,
                ordinal=ordinal,
                payload=rule.as_payload(),
                confidence=rule.confidence,
            )
            self.s.add(row)
            rows.append(row)
        pr.rule_count = len(rows)
        if ruleset.confidence is not None:
            pr.confidence = ruleset.confidence
        job.status = "extracted"
        await self.s.flush()
        return rows

    # ---- helpers -------------------------------------------------------------
    async def _get_job(self, job_id: uuid.UUID) -> ImportJob:
        job = await self.s.get(ImportJob, job_id)
        if job is None:
            raise NotFound("Import job not found")
        return job

    async def _latest_succeeded_parse(self, job_id: uuid.UUID) -> ParseResult | None:
        # Bound to a declared name: `AsyncSession.scalar` is typed `-> Any`.
        parse_result: ParseResult | None = await self.s.scalar(
            select(ParseResult)
            .where(ParseResult.import_job_id == job_id, ParseResult.status == "succeeded")
            .order_by(ParseResult.created_at.desc())
        )
        return parse_result

    def _resolve_parser(self, job: ImportJob) -> BaseParser:
        if job.parser_name:
            return self.registry.resolve_by_name(job.parser_name, job.parser_version)
        source = "generic"
        return self.registry.resolve(source=source, fmt=job.format)
