"""run_logging.py — file logging for scheduled entrypoints (#271).

A scheduled task's console output vanishes with its window: when a run
misbehaves, the log file is the only witness. Every scheduled entrypoint
(executor, fill check, flex audit, gateway smoke) calls setup_run_logging()
instead of bare basicConfig, adding a rotating file handler alongside the
console handler. Directory defaults to ./logs (gitignored); override with
BASIS_LOG_DIR.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_run_logging(name: str) -> None:
    """Console + rotating file handler (5 MB × 3) for entrypoint *name*."""
    logging.basicConfig(level=logging.INFO, format=_FORMAT)
    log_dir = Path(os.getenv("BASIS_LOG_DIR", "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError as exc:  # an unwritable disk must not stop the trading run
        logging.getLogger(__name__).warning("File logging unavailable (%s): %s", log_dir, exc)
        return
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
