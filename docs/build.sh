#!/usr/bin/env bash
# Build the documentation site into docs/_build/html.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export LC_ALL="${LC_ALL:-C.UTF-8}"
sphinx-build -b html -W --keep-going . _build/html "$@"
echo "Built docs to: $(pwd)/_build/html"
