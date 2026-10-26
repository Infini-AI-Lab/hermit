#!/usr/bin/env bash
# Assemble the project homepage and Sphinx docs into one GitHub Pages artifact.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

site_dir="$(pwd)/docs/_build/site"
rm -rf -- "$site_dir"
export HERMIT_DOCS_INCLUDE_DRAFTS=0
python3 -m sphinx -b html -W --keep-going -d docs/_build/doctrees-pages docs "$site_dir/docs" "$@"
# Sphinx can create an empty excluded static directory while walking its assets.
rm -rf -- "$site_dir/docs/_static/drafts"

cp docs/web/index.html "$site_dir/index.html"
mkdir -p "$site_dir/assets"
cp -R assets/. "$site_dir/assets/"
cp docs/web/assets/lean-collaboration.gif "$site_dir/assets/"
touch "$site_dir/.nojekyll"

echo "Built project site to: $site_dir"
