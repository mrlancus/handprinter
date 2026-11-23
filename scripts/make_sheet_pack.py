"""Build the printable writing-sheet pack: handprinter/sheet_pack.json, then the PDF.

    python scripts/make_sheet_pack.py          # JSON (deterministic, from the line banks)
    node scripts/sheet_pack_pdf.js             # PDF with maths rendered (needs Playwright)

Never change a published pack's lines: pages you already wrote are matched against them.
Add a new pack with new numbers instead.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handprinter import sheets  # noqa: E402

out = ROOT / "handprinter" / sheets.PACK_FILE
if out.exists() and "--force" not in sys.argv:
    sys.exit(f"{out} exists; its sheets may already be written. Use --force only if nobody has printed it.")
pack = {"pack": 1, "note": "Printed writing sheets. Do not edit: written pages are matched against these lines.",
        "sheets": sheets.build_pack()}
out.write_text(json.dumps(pack, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {len(pack['sheets'])} sheets to {out}")
