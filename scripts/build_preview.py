#!/usr/bin/env python3
"""Maakt preview.html: index.html met de data er direct in (voor gebruik zonder server)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "preview.html"
bonus_file = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "bonus.json"
html = (ROOT / "index.html").read_text(encoding="utf-8")
data = {
    "prices": json.loads((ROOT / "data/prices.json").read_text(encoding="utf-8")),
    "bonus": json.loads(bonus_file.read_text(encoding="utf-8")),
    "status": json.loads((ROOT / "data/status.json").read_text(encoding="utf-8")),
}
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html = html.replace("<script>\n(() => {", f'<script id="embed" type="application/json">{blob}</script>\n<script>\n(() => {{', 1)
out.write_text(html, encoding="utf-8")
print(out, round(out.stat().st_size / 1e6, 1), "MB")
