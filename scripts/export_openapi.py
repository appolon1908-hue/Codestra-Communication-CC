from __future__ import annotations

import json
from pathlib import Path

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "contracts" / "openapi.v1.json"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"COMMUNICATION_OPENAPI_EXPORTED={TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
