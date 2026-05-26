from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


load_env_file(ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    start_date = os.environ.get("REPORT_START_DATE", "2026-05-01")
    end_date = os.environ.get("REPORT_END_DATE", date.today().isoformat())
    out_dir = os.environ.get("REPORT_OUT_DIR", "public/reports/omen_draft_3_0")
    custom_cards_file = os.environ.get("CUSTOM_CARDS_FILE", "resources/OMN_Draft_2.txt")

    command = [
        sys.executable,
        str(ROOT / "scripts" / "omen_draft_report.py"),
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--out-dir",
        out_dir,
    ]
    if custom_cards_file:
        command.extend(["--custom-cards-file", custom_cards_file])
    if env_bool("SKIP_IMAGES", True):
        command.append("--skip-images")
    if env_bool("INCLUDE_NON_OMEN", False):
        command.append("--include-non-omen")

    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
