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


REDACTED = "[redacted]"


class RedactingFilter(logging.Filter):
    """Replaces every occurrence of each secret in a record's rendered text —
    message, exception and stack — before any handler formats it (#1101).

    A HANDLER filter, deliberately: a logger's own filters never see records
    that propagate up from child loggers (ib_async.wrapper, ...), but every
    record reaching a handler passes the handler's filters. The record is
    rewritten in place (msg pre-rendered, args cleared, exc_text pre-rendered)
    so whichever handler runs first, the others see only redacted text."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # a malformed %-format: keep the raw template, redacted
            rendered = str(record.msg)
        record.msg = self._redact(rendered)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def secure_live_logging(secrets: list[str]) -> None:
    """The live process's log hygiene (#1101). ib_async logs Position,
    openOrder, orderStatus and execDetails at INFO with the account id in
    them, so: its logger goes to WARNING, and every handler already attached
    (the console and the rotating run log) gets a RedactingFilter for
    *secrets* (the live account id), which catches anything that still gets
    through, at any level, from any module. Call after setup_run_logging."""
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    redactor = RedactingFilter(secrets)
    loggers = [logging.getLogger(), *(lg for lg in logging.root.manager.loggerDict.values())]
    for lg in loggers:
        for handler in getattr(lg, "handlers", []):
            handler.addFilter(redactor)
