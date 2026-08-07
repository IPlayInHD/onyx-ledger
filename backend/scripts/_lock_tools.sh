# Shared: build an ISOLATED, pinned environment for compiling the lock.
#
# Sourced by lock_dependencies.sh and check_lock.sh. Sets $PIP_COMPILE.
#
# Why not just use the ambient pip:
#
#   1. pip-tools imports `pip._internal.utils.compat.stdlib_pkgs`, which pip 26
#      removed. On a GitHub runner (pip 26.2.1) every pip-tools invocation dies
#      with an ImportError before it compiles anything — so whether the lock
#      gate runs at all depended on which pip the machine happened to ship.
#   2. A lock is only reproducible if the tool that produced it is pinned too.
#      Pinning the interpreter and leaving the resolver floating is half a
#      guarantee.
#
# The environment is built in a temp directory and removed by the caller's trap.

_LOCK_TOOLS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read -r _PINNED_PIP _PINNED_PIP_TOOLS <<EOF
$(python - <<'PY'
import tomllib
with open("pyproject.toml", "rb") as fh:
    gate = tomllib.load(fh)["tool"]["onyx"]["quality_gate"]
print(gate["lock_tool_pip"], gate["lock_tool_pip_tools"])
PY
)
EOF

# The caller removes LOCK_TOOLS_DIR in its EXIT trap.
LOCK_TOOLS_DIR="$(mktemp -d)"
LOCK_TOOLS_VENV="${LOCK_TOOLS_DIR}/locktools"
python -m venv "$LOCK_TOOLS_VENV"
"$LOCK_TOOLS_VENV/bin/pip" install --quiet --disable-pip-version-check "pip==${_PINNED_PIP}"
"$LOCK_TOOLS_VENV/bin/pip" install --quiet --disable-pip-version-check "pip-tools==${_PINNED_PIP_TOOLS}"

# `python -m piptools compile`, so the pinned interpreter is unambiguous.
PIP_COMPILE=("$LOCK_TOOLS_VENV/bin/python" -m piptools compile
  --quiet
  --generate-hashes      # every artifact is content-verified at install time
  --strip-extras         # extras are resolved here, not re-resolved at install
  --no-header            # the invocation is documented in the scripts, not the artifact
)

echo "lock tooling: pip ${_PINNED_PIP}, pip-tools ${_PINNED_PIP_TOOLS} (isolated)"
