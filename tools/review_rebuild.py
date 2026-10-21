"""Explicitly release the one-time Bark hold after reviewing a fresh database.

Only the user-triggered approve_rebuild action should call this.
No Bark sends, metadata edits, or external requests take place.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def approve(root: Path) -> bool:
    path = Path(root) / "state" / "rebuild_review_hold.json"
    if not path.is_file():
        return False
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or value.get("requires_review") is not True:
        raise ValueError("Invalid rebuild review marker; refusing removal")
    path.unlink()
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Allow Bark sending AFTER examining the new library")
    parser.add_argument("--approve", action="store_true", required=True)
    parser.parse_args(argv)
    changed = approve(ROOT)
    print("Bark unlocked; no notification was sent." if changed else
          "No review hold was present; no notification was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
