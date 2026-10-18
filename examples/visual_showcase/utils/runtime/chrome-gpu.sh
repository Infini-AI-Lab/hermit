#!/usr/bin/env bash
# Launch Playwright's Chromium with WebGL on the NVIDIA GPU (ANGLE over EGL). Under Chromium's default SwiftShader
# software path every WebGL canvas on this machine captures as a blank white image with no error.
#
# The binary comes from $VISUAL_CHROME_BIN, else $PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux64/chrome, else
# /scratch/haizhonz/.cache/ms-playwright/chromium-*/chrome-linux64/chrome: that is the realpath of ~/.cache/ms-playwright
# on this machine (~/.cache is a symlink). $HOME is never expanded here: inside the sandbox HOME is /tmp/home.
# When $VISUAL_TRACE names a file, one line "launch <utc> <pid>" is appended to it per launch (the harness reads it).
set -u
BIN="${VISUAL_CHROME_BIN:-}"
if [ -z "$BIN" ] && [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
  for candidate in "$PLAYWRIGHT_BROWSERS_PATH"/chromium-*/chrome-linux64/chrome; do   # last match = highest build
    [ -x "$candidate" ] && BIN="$candidate"
  done
fi
if [ -z "$BIN" ]; then
  for candidate in /scratch/haizhonz/.cache/ms-playwright/chromium-*/chrome-linux64/chrome; do
    [ -x "$candidate" ] && BIN="$candidate"
  done
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
  echo "chrome-gpu.sh: no Chromium binary found (set VISUAL_CHROME_BIN)" >&2
  exit 127
fi
if [ -n "${VISUAL_TRACE:-}" ]; then
  printf 'launch %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" >> "$VISUAL_TRACE" 2>/dev/null || true
fi
exec "$BIN" --use-gl=angle --use-angle=gl-egl --ignore-gpu-blocklist "$@"
