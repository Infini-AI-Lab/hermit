#!/usr/bin/env bash
# Train, or evaluate, ASPIRE on Robosuite: the cap-harness engine's launcher (../../cap_harness/launch.sh, whose header
# gives the commands and variables) with this folder's configs, e.g. ASPIRE_ROOT=/path/to/ASPIRE ./launch.sh
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ASPIRE_EXAMPLE="$HERE/.." ASPIRE_METHOD=aspire exec "$HERE/../../cap_harness/launch.sh" "$@"
