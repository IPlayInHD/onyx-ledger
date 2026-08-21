"""The production image must install what the gate tested, and nothing else.

TWO DEFECTS THIS PINS.

1. **It installed from the declaration, not the lock.** `pip install .` resolves
   the version RANGES in `pyproject.toml` at build time, so two images built
   from the same commit could contain different dependency trees — and neither
   would match the tree the quality gate verified, because the gate installs
   `--require-hashes` from the lock. The README already claimed development, CI
   and production resolved identically. They did not.

2. **It pinned the wrong interpreter.** The image was `python:3.12-slim` while
   the project declares `requires-python = "==3.11.*"`. That is not a style
   difference: pip enforces `requires-python`, so the build could not have
   succeeded. The image was broken and unpinned at the same time, which is how
   an unbuilt image stays unnoticed.

Read statically rather than by building. A Docker build needs a daemon and a
network; the properties worth defending are visible in the file, and a test that
needs neither runs in every gate.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DOCKERFILE = BACKEND / "deploy" / "Dockerfile"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions() -> list[str]:
    """Dockerfile lines with comments and continuations folded away."""
    text = re.sub(r"\\\s*\n", " ", _dockerfile())
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_dockerfile_exists_to_be_checked() -> None:
    """Non-vacuity for every regex below."""
    assert DOCKERFILE.is_file()
    assert any(line.startswith("FROM ") for line in _instructions())


def test_the_interpreter_matches_the_one_supported_runtime() -> None:
    """One runtime, declared once, honoured everywhere.

    Determinism of dependency resolution, of canonical serialization and of the
    sealed hashes is measured on 3.11 and nowhere else.
    """
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    series = re.fullmatch(r"==(\d+\.\d+)\.\*", requires)
    assert series, f"unexpected requires-python {requires!r}; update this test deliberately"
    expected = series.group(1)

    bases = [line for line in _instructions() if line.startswith("FROM python:")]
    assert bases, "the image must build on an explicit python base"
    for line in bases:
        assert f"python:{expected}" in line, (
            f"{line!r} does not match requires-python {requires!r}. "
            "pip enforces requires-python, so a mismatched base cannot build."
        )


def test_dependencies_are_installed_from_the_hash_pinned_lock() -> None:
    text = _dockerfile()
    assert "--require-hashes" in text, (
        "the production image must verify every artifact against the committed "
        "hashes; without it a re-uploaded package can be substituted silently"
    )
    assert "requirements.lock.txt" in text, "the runtime lock is the source of dependencies"


def test_the_declaration_is_never_resolved_at_build_time() -> None:
    """`pip install .` without `--no-deps` is the whole defect in one line."""
    offenders = [
        line
        for line in _instructions()
        if re.search(r"\bpip install\b", line)
        and re.search(r"(?<!-)\s\.$|\s\.\s", line)  # installs the local package
        and "--no-deps" not in line
    ]
    assert not offenders, (
        "installing the local package without --no-deps lets pip resolve "
        f"pyproject.toml's ranges behind the lock's back: {offenders}"
    )


def test_the_image_does_not_run_as_root() -> None:
    """Unchanged by this entry, asserted because it is easy to lose in a
    rewrite and expensive to notice afterwards."""
    instructions = _instructions()
    users = [line for line in instructions if line.startswith("USER ")]
    assert users, "the runtime stage must drop privileges"
    assert users[-1].split()[1] != "root"
