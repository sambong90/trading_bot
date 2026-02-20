#!/usr/bin/env bash
# Collect recent logs and important files into archive for debugging
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
OUT="$DIR/logs/archive/debug_$(date +%Y%m%d_%H%M%S).tar.gz"
FILES=(logs/*.log logs/*.json logs/*.txt logs/*.csv trading_bot/*.py trading_bot/*.db)
mkdir -p "$DIR/logs/archive"
# create tar gz of available files
tar -czf "$OUT" ${FILES[@]} 2>/dev/null || true
echo "Created archive: $OUT"
ls -lh "$OUT"
