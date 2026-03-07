#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  DealPulses — Run DealRadar locally & sync output to site root
#
#  Usage:
#    ./run_dealradar.sh            # Normal scan
#    ./run_dealradar.sh --digest   # Force full digest email
#    ./run_dealradar.sh --top 10   # Print top 10 deals, no email
#    ./run_dealradar.sh --test     # Test SMTP config
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RADAR_DIR="$SCRIPT_DIR/dealradar"

echo "🔍 DealPulses DealRadar — $(date '+%Y-%m-%d %H:%M:%S')"
echo "───────────────────────────────────────────────"

# ── Activate virtual env if it exists ────────────────────────────
if [ -d "$RADAR_DIR/venv" ]; then
    source "$RADAR_DIR/venv/bin/activate"
    echo "✅ Virtual env activated"
else
    echo "⚠️  No venv found — using system Python"
    echo "   Tip: cd dealradar && python -m venv venv && pip install -r requirements.txt"
fi

# ── Run DealRadar ─────────────────────────────────────────────────
cd "$RADAR_DIR"
python dealradar.py "$@"

# ── Sync deals.json to site root ─────────────────────────────────
if [ -f "$RADAR_DIR/deals.json" ]; then
    cp "$RADAR_DIR/deals.json" "$SCRIPT_DIR/deals.json"
    DEAL_COUNT=$(python3 -c "import json; d=json.load(open('deals.json')); print(len(d))" 2>/dev/null || echo "?")
    echo ""
    echo "✅ deals.json copied to site root ($DEAL_COUNT deals)"
else
    echo "⚠️  dealradar/deals.json not found — nothing copied"
fi

# ── Sync price_history/ if generated ─────────────────────────────
if [ -d "$RADAR_DIR/price_history" ]; then
    mkdir -p "$SCRIPT_DIR/price_history"
    cp -r "$RADAR_DIR/price_history/." "$SCRIPT_DIR/price_history/"
    echo "✅ price_history/ synced"
fi

echo ""
echo "Done! Open index.html in a browser to see updated deals."
