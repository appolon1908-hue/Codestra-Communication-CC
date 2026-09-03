from __future__ import annotations

import json
from pathlib import Path

from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    committed = json.loads((ROOT / "contracts" / "openapi.v1.json").read_text(encoding="utf-8"))
    runtime = app.openapi()
    if committed != runtime:
        raise SystemExit("COMMUNICATION_OPENAPI_PARITY=FAIL")
    print("COMMUNICATION_OPENAPI_PARITY=PASS")


if __name__ == "__main__":
    main()
