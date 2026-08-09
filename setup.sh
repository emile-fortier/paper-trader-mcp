#!/bin/sh
set -eu
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  UV=uv
elif [ -x .tools/uv ]; then
  UV=.tools/uv
else
  echo "Error: uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  "$UV" venv --python 3.14 .venv
fi
"$UV" sync --frozen
echo "Installed. Configure APCA_API_KEY_ID/APCA_API_SECRET_KEY, then run ./run.sh"
