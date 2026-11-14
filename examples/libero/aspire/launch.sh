#!/usr/bin/env bash
# Train, or evaluate, ASPIRE on LIBERO: ../utils/launch.sh with this folder's configs (its header gives the
# commands and variables), e.g. ASPIRE_ROOT=/path/to/ASPIRE ASPIRE_CONFIG=smoke_luna.yaml ./launch.sh
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../utils/launch.sh" aspire "$@"
