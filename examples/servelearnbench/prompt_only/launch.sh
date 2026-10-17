#!/bin/bash
# The prompt-only baseline on a ServeLearnBench scenario: the MWM launcher with this directory's config
# (MW_METHOD=prompt_only), its own run tag and meter ports. Same usage and settings as ../mwm/launch.sh.
#
#   examples/servelearnbench/prompt_only/launch.sh retail_l3          # train, then the per-window tests
#   examples/servelearnbench/prompt_only/launch.sh retail_l3 test     # the tests again
HERE=$(cd "$(dirname "$0")" && pwd)
SCENARIO=${1:?usage: launch.sh <scenario> [train|test]}
MW_METHOD=prompt_only MW_TAG=${MW_TAG:-slb_po_$SCENARIO} WM_PORT=${WM_PORT:-3461} \
  exec "$HERE/../mwm/launch.sh" "$@"
