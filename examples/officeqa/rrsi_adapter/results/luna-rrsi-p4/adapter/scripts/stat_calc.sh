#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: stat_calc.sh {gm|sd-pop|sd-sample|trimmed-mean} values..." >&2
  exit 2
fi
mode=$1
shift

case "$mode" in
  gm)
    awk '
      { if ($1 <= 0) exit 2; s += log($1); n++ }
      END { if (n == 0) exit 2; printf "n=%d result=%.12f\n", n, exp(s/n) }
    ' < <(printf '%s\n' "$@")
    ;;
  sd-pop|sd-sample)
    divisor_mode=$mode awk '
      { x[n++]=$1; sum += $1 }
      END {
        if (n == 0 || (ENVIRON["divisor_mode"] == "sd-sample" && n < 2)) exit 2
        mean=sum/n; for (i=0; i<n; i++) d += (x[i]-mean)^2
        den=(ENVIRON["divisor_mode"] == "sd-sample" ? n-1 : n)
        printf "n=%d result=%.12f\n", n, sqrt(d/den)
      }
    ' < <(printf '%s\n' "$@")
    ;;
  trimmed-mean)
    if [ "$#" -lt 3 ]; then echo "trimmed-mean needs percent and values" >&2; exit 2; fi
    percent=$1
    shift
    sort -n < <(printf '%s\n' "$@") | awk -v p="$percent" '
      { x[n++]=$1 }
      END {
        if (n == 0 || p < 0 || p >= 50) exit 2
        drop=n*p/100
        if (drop != int(drop)) exit 2
        lo=drop; hi=n-drop; sum=0; count=0
        for (i=lo; i<hi; i++) { sum += x[i]; count++ }
        if (count == 0) exit 2
        printf "n=%d retained=%d result=%.12f\n", n, count, sum/count
      }
    '
    ;;
  *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
