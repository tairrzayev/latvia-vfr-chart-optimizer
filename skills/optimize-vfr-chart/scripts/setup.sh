#!/usr/bin/env bash
# Create (or reuse) the Python environment for this skill and print the path of its python.
# The environment lives in the user cache directory, outside the skill folder, and is rebuilt
# whenever requirements.txt changes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/optimize-vfr-chart"
venv="$cache/venv"
req="$here/requirements.txt"
stamp="$venv/.requirements"

if [ ! -x "$venv/bin/python" ] || ! cmp -s "$req" "$stamp"; then
  mkdir -p "$cache"
  rm -rf "$venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet --python ">=3.10" "$venv" >&2
    uv pip install --quiet --python "$venv/bin/python" -r "$req" >&2
  else
    python3 -m venv "$venv" >&2
    "$venv/bin/python" -m pip install --quiet --upgrade pip >&2
    "$venv/bin/python" -m pip install --quiet -r "$req" >&2
  fi
  cp "$req" "$stamp"
fi

"$venv/bin/python" -c 'import pikepdf, shapely, numpy, pymupdf, pypdfium2, PIL' >&2
echo "$venv/bin/python"
