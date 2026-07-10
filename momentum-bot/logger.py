"""Structured logging + optional trade notifications.

``get_logger`` returns a stdlib logger configured for either human-readable or
JSON output. ``Notifier`` fans trade/kill-switch events out to Discord and/or
Telegram webhooks if their env vars are set; it never raises into the caller.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import urllib.error
import urllib.request


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach structured extras stashed on the record.
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_CONFIGURED = False


def get_logger(name: str = "momentum-bot", level: str = "INFO",
               json_output: bool = False) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        if json_output:
            handler.setFormatter(_JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            ))
        logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True
    logger.setLevel(getattr(logging, level, logging.INFO))
    return logger


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Log a message with structured key/value fields attached."""
    logger.log(level, msg, extra={"extra_fields": fields})


class Notifier:
    """Best-effort webhook notifier. Failures are logged, never raised."""

    def __init__(self, config, logger: logging.Logger) -> None:
        self.discord = config.discord_webhook_url
        self.tg_token = config.telegram_bot_token
        self.tg_chat = config.telegram_chat_id
        self.log = logger
        self.enabled = bool(self.discord or (self.tg_token and self.tg_chat))

    def send(self, title: str, body: str) -> None:
        if not self.enabled:
            return
        text = f"**{title}**\n{body}"
        if self.discord:
            self._post_json(self.discord, {"content": text})
        if self.tg_token and self.tg_chat:
            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
            self._post_json(url, {"chat_id": self.tg_chat, "text": f"{title}\n{body}"})

    def _post_json(self, url: str, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError, ValueError) as exc:  # pragma: no cover
            self.log.warning("notifier post failed: %s", exc)
