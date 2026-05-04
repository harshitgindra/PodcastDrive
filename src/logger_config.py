"""Centralized logging configuration for playlist-downloader.

Call ``setup_logging()`` once at application startup (in sync_podcast.py or
any other entry point).  AWS Lambda already ships logs to CloudWatch, so the
rotating file handler is only attached when running locally.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

# Detect Lambda environment — file logging is skipped there.
_IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

LOG_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: str = "./logs",
    log_level: str = "INFO",
    retention_days: int = 30,
) -> None:
    """Configure the root logger with a console handler and (locally) a
    rotating daily file handler.

    Args:
        log_dir:        Directory where log files are written.  Created
                        automatically if it does not exist.  Ignored on Lambda.
        log_level:      Logging level string, e.g. ``"INFO"`` or ``"DEBUG"``.
                        Can also be set via the ``LOG_LEVEL`` env var.
        retention_days: How many daily log files to keep before rotating them
                        away.  Can also be set via the ``LOG_RETENTION_DAYS``
                        env var.
    """
    # Allow env-var overrides so cron / launchd jobs can tune without code changes.
    log_level = os.environ.get("LOG_LEVEL", log_level).upper()
    retention_days = int(os.environ.get("LOG_RETENTION_DAYS", retention_days))
    log_dir = os.environ.get("LOG_DIR", log_dir)

    numeric_level = getattr(logging, log_level, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers if setup_logging() is called more than once.
    if root_logger.handlers:
        return

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # --- Console handler (always active) ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # --- Rotating file handler (local runs only) ---
    if not _IS_LAMBDA:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "playlist_downloader.log")

        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",       # rotate at midnight
            interval=1,            # every 1 day
            backupCount=retention_days,
            encoding="utf-8",
            utc=False,
        )
        # Rotated files get a date suffix: playlist_downloader.log.YYYY-MM-DD
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        logging.getLogger(__name__).info(
            "File logging enabled → %s (rotating daily, keeping %d days)",
            log_file,
            retention_days,
        )
