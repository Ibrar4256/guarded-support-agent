"""Export refund-api's OpenAPI spec to contracts/ (ADR-007 contract test).

    uv run python scripts/export_openapi.py           # write the committed file
    uv run python scripts/export_openapi.py --check   # CI: fail if it differs

The spec comes from the production app only; test routes are never part of the contract.
Sorted keys and a trailing newline keep the committed file stable, so any contract change
shows up as a reviewable diff.
"""

import argparse
import difflib
import json
import sys
from datetime import timedelta
from pathlib import Path

from refund_api.app import create_app
from refund_api.config import MIN_TOKEN_LENGTH, Settings

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "refund_api.openapi.json"

# Building the app needs settings, but exporting the spec never connects or authenticates.
EXPORT_SETTINGS = Settings(
    database_url="postgresql+psycopg://export:unused@127.0.0.1:1/unused",
    tokens=("export-only-" + "x" * MIN_TOKEN_LENGTH,),
    processing_budget=timedelta(seconds=5),
    key_retention=timedelta(hours=24),
    max_retry_window=timedelta(minutes=10),
    max_recovery_delay=timedelta(hours=1),
    pool_timeout=timedelta(seconds=2),
    app_env="export",
)


def render() -> str:
    spec = create_app(EXPORT_SETTINGS).openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export or check refund-api's OpenAPI spec.")
    parser.add_argument("--check", action="store_true", help="fail if the file differs")
    parser.add_argument("--path", type=Path, default=CONTRACT, help="contract file")
    args = parser.parse_args(argv)
    current = render()
    if not args.check:
        args.path.write_text(current, encoding="utf-8")
        print(f"wrote {args.path}")
        return 0
    committed = args.path.read_text(encoding="utf-8") if args.path.exists() else ""
    if committed == current:
        print(f"{args.path.name} matches the code")
        return 0
    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=f"{args.path.name} (committed)",
        tofile=f"{args.path.name} (from code)",
    )
    sys.stdout.writelines(diff)
    print(
        f"\n{args.path.name} is out of date: run scripts/export_openapi.py and commit it",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
