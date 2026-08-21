"""The retired JavaScript tax engine must never be deployable again.

WHY THIS IS A SECURITY TEST AND NOT A TIDINESS ONE.

`legacy/server/` is a complete second application: its own Express API, its own
tax engine in JavaScript, its own accounts, and its own storage in Netlify
Blobs. Until it was archived, the repository's root `netlify.toml` deployed
*that* tree and routed every `/api/*` request to it, while the certified
FastAPI backend was not deployed at all. Connecting the repository to Netlify —
which the old `NETLIFY.md` gave step-by-step instructions to do — would have put
an ungoverned tax engine in front of real people.

Two distinct harms, either one sufficient:

1. **A second tax authority.** Two independent implementations of Canadian tax
   law cannot both be right, and nothing reconciles them. A customer served by
   the JavaScript one would see figures no certified, replayable, gated run ever
   produced.

2. **A second identity store.** The privacy specification records this as
   PD-14: `legacy/server/store.js` held email addresses, bcrypt password hashes,
   profiles and documents outside the governed data lifecycle, so a deletion
   performed by the real product would never have reached it.

The failure mode this guards against is not someone deciding to relaunch the
prototype. It is a build configuration quietly pointing back at it — a restored
`base = "server"`, a new workflow that runs `npm --prefix legacy/server start`,
a copied deploy file. Configuration drifts; this test does not.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

#: Everything a deploy could be defined by. A file added here that references
#: the legacy tree is exactly the mistake this module exists to catch.
DEPLOY_CONFIG_GLOBS = (
    "netlify.toml",
    "vercel.json",
    "render.yaml",
    "fly.toml",
    "Procfile",
    "app.yaml",
    "*.tf",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "backend/deploy/*",
    "frontend/netlify.toml",
)

#: Paths that would mean the retired application is being built or served.
LEGACY_REFERENCES = re.compile(
    r"""
    (?<!legacy/)\bserver/           # `server/` that is not `legacy/server/`
  | (?<!legacy/)\bstatic-site/
  | base\s*=\s*["']server["']
  | netlify/functions
  | \bserver\.js\b
  | onyx-ledger-server
    """,
    re.VERBOSE,
)


def _deploy_files() -> list[Path]:
    found: list[Path] = []
    for pattern in DEPLOY_CONFIG_GLOBS:
        found.extend(p for p in REPO.glob(pattern) if p.is_file())
    return found


def test_deploy_configuration_exists_to_be_checked() -> None:
    """Non-vacuity.

    Every assertion below is "no deploy file mentions the legacy tree", which a
    repository with no deploy files would satisfy perfectly while proving
    nothing. This is the control that keeps the rest honest.
    """
    files = _deploy_files()
    assert files, "no deployment configuration found — the guard below would be vacuous"
    assert (REPO / "netlify.toml").is_file(), "the root netlify.toml is the historical deploy target"


@pytest.mark.parametrize("path", _deploy_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_deploy_configuration_references_the_legacy_engine(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Strip comments so the archival note in netlify.toml, which necessarily
    # NAMES what it decommissioned, does not read as a reference to it.
    uncommented = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("#", "//"))
    )
    match = LEGACY_REFERENCES.search(uncommented)
    assert match is None, (
        f"{path.relative_to(REPO)} references the retired JavaScript tax engine "
        f"({match.group(0)!r} at offset {match.start()}). Production must have exactly "
        "one customer tax authority: the certified FastAPI backend."
    )


def test_the_legacy_tree_is_archived_and_marked() -> None:
    """It must stay where it is, and say what it is.

    Kept rather than deleted because the privacy specification still refers to
    these files when describing what the retired application stored. A reader
    who finds the code must immediately learn it is not the product.
    """
    legacy = REPO / "legacy"
    assert legacy.is_dir(), "the retired prototype should be archived under legacy/"
    assert not (REPO / "server").exists(), "server/ must not reappear at the repository root"
    assert not (REPO / "static-site").exists(), "static-site/ must not reappear at the root"

    readme = (legacy / "README.md").read_text(encoding="utf-8")
    assert "DECOMMISSIONED" in readme
    assert "Do not deploy" in readme


def test_the_root_deploy_target_is_the_certified_frontend() -> None:
    """Positive control: it is not enough that the old target is gone.

    An empty or absent configuration also passes "does not mention server/",
    and would let a platform fall back to auto-detection — which, in a
    repository with no root package.json, can end up publishing the source tree
    itself. The deploy target has to be something, and that something is the
    certified frontend.
    """
    netlify = (REPO / "netlify.toml").read_text(encoding="utf-8")
    assert 'base = "frontend"' in netlify, "the deploy base must be the certified frontend"
    assert 'publish = "dist"' in netlify
