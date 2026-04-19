import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from ingest.pipeline import PipelineValidationError, run_pipeline, validate_sqlite_source

TIMEZONE_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))


def _line(message: str) -> str:
    stamp = datetime.now(ZoneInfo(TIMEZONE_NAME)).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{stamp}] {message}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Skimmer ingest + normalization worker.")
    parser.add_argument("--sqlite", default=os.getenv("SKIMMER_DB_PATH"), help="Path to the Skimmer SQLite export file.")
    parser.add_argument("--source-system", default=os.getenv("INGEST_SOURCE_SYSTEM", "skimmer"))
    parser.add_argument("--trigger-reason", default="cli")
    parser.add_argument("--trigger-metadata-json", default="")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.sqlite:
        print(_line("ERROR: missing SQLite source path. Set SKIMMER_DB_PATH or pass --sqlite."))
        sys.exit(1)

    try:
        if args.validate_only:
            result = validate_sqlite_source(args.sqlite, source_system=args.source_system)
            if result.get("fatals"):
                print(_line(json.dumps(result, indent=2, sort_keys=True, default=str)))
                sys.exit(2)
            print(_line(json.dumps(result, indent=2, sort_keys=True, default=str)))
            return

        trigger_metadata = {"invoked_by": "cli"}
        if args.trigger_metadata_json:
            parsed = json.loads(args.trigger_metadata_json)
            if isinstance(parsed, dict):
                trigger_metadata = parsed

        result = run_pipeline(
            args.sqlite,
            source_system=args.source_system,
            trigger_reason=args.trigger_reason,
            trigger_metadata=trigger_metadata,
        )
    except PipelineValidationError as exc:
        print(_line(f"VALIDATION ERROR: {exc}"))
        sys.exit(2)
    except Exception as exc:
        print(_line(f"ERROR: {exc}"))
        sys.exit(1)

    print(_line(json.dumps(result, indent=2, sort_keys=True, default=str)))


if __name__ == "__main__":
    main()
