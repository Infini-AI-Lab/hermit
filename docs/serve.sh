#!/usr/bin/env bash
# Live-reloading preview at http://127.0.0.1:${1:-8000}.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
sphinx-autobuild . _build/html --host 127.0.0.1 --port "${1:-8000}"
