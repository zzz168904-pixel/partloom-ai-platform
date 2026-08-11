from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from command_runner import CommandRunner
from models import DrawingRequest
from sw_connector import SolidWorksConnector


ROOT_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT_DIR / "logs"


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"solidworks_mvp_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def load_request(path: Path) -> DrawingRequest:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return DrawingRequest.from_dict(data)
    except Exception as exc:
        raise RuntimeError(f"Failed to load request JSON: {path}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SolidWorks drawing automation MVP using Python COM."
    )
    parser.add_argument(
        "request_json",
        type=Path,
        help="Path to a JSON task file, for example examples/drawing_request.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = setup_logging()
    logger = logging.getLogger(__name__)

    try:
        logger.info("Log file: %s", log_file)
        request = load_request(args.request_json)
        runner = CommandRunner(SolidWorksConnector())
        runner.run(request)
        print(f"OK: PDF exported to {request.output_path}")
        print(f"Log: {log_file}")
        return 0
    except Exception as exc:
        logger.exception("Automation failed.")
        print(f"ERROR: {exc}")
        print(f"Log: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
