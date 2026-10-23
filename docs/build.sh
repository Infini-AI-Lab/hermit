#!/usr/bin/env bash
# Build the documentation site into docs/_build/html.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export LC_ALL="${LC_ALL:-C.UTF-8}"
export HERMIT_DOCS_INCLUDE_DRAFTS=0
# Sphinx retains old static files, so remove drafts left by a local preview.
rm -rf -- _build/html/_static/drafts
sphinx-build -b html -W --keep-going . _build/html "$@"
echo "Built docs to: $(pwd)/_build/html"
