#!/usr/bin/env bash
set -u

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  printf 'usage: %s FILE CENTER [RADIUS]\n' "$0" >&2
  exit 2
fi

file=$1
center=$2
radius=${3:-12}
case "$center" in ''|*[!0-9]*) printf 'CENTER must be a positive line number\n' >&2; exit 2;; esac
case "$radius" in ''|*[!0-9]*) printf 'RADIUS must be a nonnegative integer\n' >&2; exit 2;; esac
[ "$center" -gt 0 ] || { printf 'CENTER must be positive\n' >&2; exit 2; }
[ -f "$file" ] || { printf 'file not found: %s\n' "$file" >&2; exit 1; }

lo=$((center - radius))
[ "$lo" -gt 1 ] || lo=1
hi=$((center + radius))
awk -v lo="$lo" -v hi="$hi" 'NR >= lo && NR <= hi { printf "%d:%s\n", NR, $0 }' "$file"
