import json
import logging
from datetime import UTC, datetime
from logging.config import dictConfig
from typing import Any

_STANDARD_ATTRIBUTES = frozenset(
    {
        *logging.makeLogRecord({}).__dict__,
        "asctime",
        "message",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_ATTRIBUTES and not key.startswith("_")
            }
        )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(log_level: str) -> None:
    handler = {
        "class": "logging.StreamHandler",
        "formatter": "json",
        "stream": "ext://sys.stdout",
    }
    logger = {
        "handlers": ["default"],
        "level": log_level,
        "propagate": False,
    }
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {"default": handler},
            "root": {"handlers": ["default"], "level": log_level},
            "loggers": {
                "uvicorn": logger,
                "uvicorn.error": logger,
                "uvicorn.access": logger,
            },
        }
    )
