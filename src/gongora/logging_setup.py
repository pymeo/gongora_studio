"""Logging con redaccion obligatoria.

Todo registro pasa por `RedactingFilter`. Si un secreto llega a un mensaje de
log por descuido, se enmascara antes de escribirse en disco.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gongora import redaction

LOGGER_NAME = "gongora"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redaction.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redaction.redact_value(record.args)
            else:
                record.args = tuple(
                    redaction.redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def configure(log_dir: Path, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if logger.handlers:
        return logger

    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactingFilter()

    file_handler = logging.FileHandler(log_dir / "gongora.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    file_handler.addFilter(redactor)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    stream_handler.addFilter(redactor)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
