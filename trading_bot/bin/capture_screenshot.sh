#!/usr/bin/env bash
# Placeholder: capture screenshot of local dashboard (requires wkhtmltoimage or screencapture)
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$DIR/logs/archive/screenshot_$(date +%Y%m%d_%H%M%S).png"
# Try macOS screencapture of main window - user may need to open http://127.0.0.1:5000/decisions
if command -v screencapture >/dev/null 2>&1; then
  echo "Capturing full screen to $OUT (you can crop later)"
  screencapture -x "$OUT"
  echo "Saved: $OUT"
else
  echo "screencapture not available. Please capture screen manually or install a CLI tool."
fi
