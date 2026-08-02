"""ParserRegistry — resolves (source, format) or name+version to a Parser.

The pipeline asks the registry for a parser; it never imports a concrete parser.
Registration is explicit and versioned, so multiple versions of a parser can
coexist and a re-parse can pin an exact (name, version).
"""
from __future__ import annotations

from app.core.exceptions import NotFound, ValidationError
from app.services.tkms.parsers.base import BaseParser


class ParserRegistry:
    def __init__(self) -> None:
        # keyed by name -> {version -> parser}; plus a (source, fmt) default map
        self._by_name: dict[str, dict[str, BaseParser]] = {}
        self._default_for: dict[tuple[str, str], BaseParser] = {}

    def register(self, parser: BaseParser, *, make_default: bool = True) -> BaseParser:
        self._by_name.setdefault(parser.name, {})[parser.version] = parser
        if make_default:
            self._default_for[(parser.source, parser.fmt)] = parser
            # also register a format-only fallback (source 'generic')
            self._default_for.setdefault(("generic", parser.fmt), parser)
        return parser

    def resolve(self, *, source: str, fmt: str) -> BaseParser:
        """Pick the best parser for a source+format, falling back to generic."""
        key = (source, fmt)
        if key in self._default_for:
            return self._default_for[key]
        generic = ("generic", fmt)
        if generic in self._default_for:
            return self._default_for[generic]
        raise NotFound(f"No parser registered for source='{source}', format='{fmt}'")

    def resolve_by_name(self, name: str, version: str | None = None) -> BaseParser:
        versions = self._by_name.get(name)
        if not versions:
            raise NotFound(f"No parser named '{name}'")
        if version is None:
            # latest by simple version string sort
            latest = sorted(versions)[-1]
            return versions[latest]
        if version not in versions:
            raise NotFound(f"Parser '{name}' has no version '{version}'")
        return versions[version]

    def available(self) -> list[dict]:
        return [
            {"name": p.name, "version": v, "source": p.source, "format": p.fmt}
            for name, versions in sorted(self._by_name.items())
            for v, p in sorted(versions.items())
        ]

    def require_format(self, fmt: str) -> None:
        if not any(p.fmt == fmt for versions in self._by_name.values() for p in versions.values()):
            raise ValidationError(f"Unsupported import format '{fmt}'")


def build_default_registry() -> ParserRegistry:
    """The standard registry: structured parsers live; PDF/HTML/finance stubbed."""
    from app.services.tkms.parsers.cra_html_parser import CRAHtmlParser
    from app.services.tkms.parsers.cra_pdf_parser import CRAPdfParser
    from app.services.tkms.parsers.csv_parser import GovernmentCsvParser
    from app.services.tkms.parsers.finance_parser import (
        DepartmentFinanceParser,
        ProvincialParser,
    )
    from app.services.tkms.parsers.json_parser import GovernmentJsonParser
    from app.services.tkms.parsers.manual_parser import ManualRuleParser
    from app.services.tkms.parsers.xml_parser import GovernmentXmlParser

    reg = ParserRegistry()
    reg.register(GovernmentCsvParser())
    reg.register(GovernmentJsonParser())
    reg.register(GovernmentXmlParser())
    reg.register(ManualRuleParser())
    # stubs: registered so the port is discoverable + reparse can select them,
    # but they raise NotImplemented until real (optionally AI-assisted) impls land.
    reg.register(CRAHtmlParser())
    reg.register(CRAPdfParser())
    reg.register(DepartmentFinanceParser())
    reg.register(ProvincialParser(), make_default=False)
    return reg
