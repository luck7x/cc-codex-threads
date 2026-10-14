#!/usr/bin/env bash
# cc-codex-threads entry point. The implementation is cct.py (Python 3.8+,
# standard library only); this wrapper keeps the documented script path.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
command -v python3 >/dev/null 2>&1 || { echo "[cct] ERROR: python3 (3.8+) is required" >&2; exit 2; }
exec python3 "$SCRIPT_DIR/cct.py" "$@"
