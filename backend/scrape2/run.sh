#!/usr/bin/env bash
# Local run: decrypt sample → output/vtrio_data.json
# Live (login cookies): ./run.sh live cookies.txt

set -e
cd "$(dirname "$0")"
PY=".venv/bin/python"
OUT="output/vtrio_data.json"

mkdir -p output

if [[ ! -x "$PY" ]]; then
  echo "venv nahi mila. Pehle chalao: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [[ "${1:-}" == "live" ]]; then
  COOKIES="${2:-cookies.txt}"
  if [[ ! -f "$COOKIES" ]]; then
    echo "cookies file nahi mili: $COOKIES"
    echo "Browser se login karke cookies.txt export karo, phir: ./run.sh live cookies.txt"
    exit 1
  fi
  echo "Live API call (vtrio)..."
  "$PY" scrape_vcasino.py --client curl_cffi --cookies "$COOKIES" -o "$OUT"
else
  echo "Local sample decrypt..."
  "$PY" scrape_vcasino.py --decrypt-only sample_response.json -o "$OUT" 2>/dev/null
fi

echo ""
echo "JSON saved: $OUT"
echo "--- preview ---"
"$PY" - <<'PY'
import json
from pathlib import Path
p = Path("output/vtrio_data.json")
d = json.loads(p.read_text())
if d.get("success") and "data" in d and isinstance(d["data"], dict) and "sub" in d["data"]:
    g = d["data"]
    print(f"game: {g.get('gtype')} | round: {g.get('mid')} | markets: {len(g.get('sub', []))}")
    for m in g.get("sub", [])[:5]:
        print(f"  - {m.get('nat')}: back={m.get('b')} lay={m.get('l')} [{m.get('gstatus')}]")
    if len(g.get("sub", [])) > 5:
        print(f"  ... +{len(g['sub'])-5} aur markets (poora file mein dekho)")
else:
    print(json.dumps(d, indent=2, ensure_ascii=False)[:800])
PY
