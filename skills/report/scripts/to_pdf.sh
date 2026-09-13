#!/usr/bin/env bash
# to_pdf.sh — deterministic headless print of a report-v2 review .html to PDF.
#
# The review page's own @media print block does the work (un-hides every .page,
# one per printed page, hides sidebar + controls); this wrapper just drives
# Chromium headless over it. Same posture as narrate/joint-browser and
# divorcio/common/deck_src/make_pdf.py. No wkhtmltopdf/weasyprint dependency.
#
# Alpine renders on load, so all pages resolve in the PDF (no blank pages).
#
# Usage: to_pdf.sh <in.html> [out.pdf]
set -euo pipefail

IN="${1:?usage: to_pdf.sh <in.html> [out.pdf]}"
OUT="${2:-${IN%.html}.pdf}"

# Resolve to absolute paths — Chromium needs a file:// URL.
IN_ABS="$(cd "$(dirname "$IN")" && pwd)/$(basename "$IN")"
OUT_ABS="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd || pwd)/$(basename "$OUT")"

# Find a Chromium/Chrome binary (env override wins).
CHROME="${CHROMIUM:-}"
if [ -z "$CHROME" ]; then
  for c in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/opt/homebrew/bin/chromium" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v chromium 2>/dev/null || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then CHROME="$c"; break; fi
  done
fi
[ -n "$CHROME" ] || { echo "to_pdf: no Chromium/Chrome found (set \$CHROMIUM)" >&2; exit 1; }

# NOTE: --headless=new + --run-all-compositor-stages-before-draw lets Alpine
# finish rendering before the snapshot. Do NOT use --virtual-time-budget here:
# under --print-to-pdf it snapshots at virtual-time expiry BEFORE Alpine's
# real-timer/microtask render completes, producing pages with empty bodies
# (verified 2026-09-13 — the exact deck_src "headless lies" trap).
"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --no-pdf-header-footer --run-all-compositor-stages-before-draw \
  --print-to-pdf="$OUT_ABS" "file://$IN_ABS" 2>/dev/null

[ -f "$OUT_ABS" ] || { echo "to_pdf: PDF not produced" >&2; exit 1; }
echo "Wrote $OUT_ABS ($(wc -c < "$OUT_ABS") bytes)"
