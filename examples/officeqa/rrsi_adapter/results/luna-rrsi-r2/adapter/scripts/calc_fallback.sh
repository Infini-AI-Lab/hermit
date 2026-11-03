#!/usr/bin/env bash
set -u

if [ "$#" -lt 2 ]; then
  printf 'usage: %s geom|mean|sd_sample|sd_population|smooth ALPHA? VALUES...\n' "$0" >&2
  exit 2
fi
mode=$1
shift

case "$mode" in
  geom|mean|sd_sample|sd_population)
    [ "$#" -gt 0 ] || { printf 'at least one value is required\n' >&2; exit 2; }
    awk -v mode="$mode" '
      { for (i=1; i<=NF; i++) { x=$i+0; n++; if (mode=="geom") { if (x<=0) exit 3; lg+=log(x) }
          else { sum+=x; sumsq+=x*x } } }
      END { if (n==0) exit 2
        if (mode=="geom") printf "%.15g\n", exp(lg/n)
        else if (mode=="mean") printf "%.15g\n", sum/n
        else { d=(mode=="sd_sample" ? n-1 : n); if (d<=0) exit 4
          v=(sumsq-sum*sum/n)/d; if (v<0 && v>-1e-12) v=0
          printf "%.15g\n", sqrt(v) } }' <<< "$*"
    ;;
  smooth)
    [ "$#" -ge 2 ] || { printf 'smooth requires ALPHA and at least one observation\n' >&2; exit 2; }
    alpha=$1
    shift
    awk -v a="$alpha" 'BEGIN { if (a<0 || a>1) exit 2 }
      { for (i=1; i<=NF; i++) { if (n++==0) s=$i+0; else s=a*($i+0)+(1-a)*s } }
      END { if (n==0) exit 2; printf "%.15g\n", s }' <<< "$*"
    ;;
  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
