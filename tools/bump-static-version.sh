#!/usr/bin/env bash
# Bump the ?v= cache-buster on every static asset reference so browsers
# fetch fresh CSS/JS after a deploy.
#
# Usage:   tools/bump-static-version.sh
# Result:  All "?v=YYYYMMDD-HHMMSS" strings in src/tesla_fleet/static/*.html
#          rewritten to the current timestamp, then git status printed.
#
# Run this before committing any change that touches static/*.css|js so
# the deploy is visible without a hard-refresh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
PATTERN='v=[0-9]\{8\}-[0-9]\{6\}'

cd "$ROOT"
changed=0
for f in src/tesla_fleet/static/*.html; do
  if grep -q "$PATTERN" "$f"; then
    sed -i '' -E "s/v=[0-9]{8}-[0-9]{6}/v=${STAMP}/g" "$f"
    changed=$((changed + 1))
  fi
done

echo "Bumped ${changed} files to v=${STAMP}"
git --no-pager diff --stat src/tesla_fleet/static/ | tail -5
