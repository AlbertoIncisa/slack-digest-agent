from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import schedule

if TYPE_CHECKING:
    from collections.abc import Callable

    from slack_digest.config import DigestConfig

logger = logging.getLogger(__name__)

LAST_RUN_FILE = Path(__file__).resolve().parent.parent.parent / ".last_digest_run"


class DigestScheduler:
    def __init__(self, callback: Callable, config: DigestConfig):
        self._callback = callback
        self._config = config
        self._running = False
        self._thread: threading.Thread | None = None
        self._schedule_job()

    def _schedule_job(self) -> None:
        schedule.clear()
        for t in self._config.digest.schedule:
            schedule.every().day.at(t).do(self._run_digest)
        logger.info(f"Scheduled daily digests at {', '.join(self._config.digest.schedule)}")

    def _run_digest(self) -> None:
        logger.info("Scheduled digest triggered")
        tz = ZoneInfo(self._config.digest.timezone)
        try:
            self._callback()
        except Exception:
            logger.exception("Scheduled digest generation failed")
        finally:
            _save_last_run(tz)

    def _check_missed(self) -> None:
        tz = ZoneInfo(self._config.digest.timezone)
        now = datetime.now(tz)
        last_run = _load_last_run()

        for t in self._config.digest.schedule:
            hour, minute = (int(x) for x in t.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= target and (not last_run or last_run < target):
                logger.info(f"Missed digest at {t} — running now")
                self._run_digest()
                return

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="digest-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            schedule.run_pending()
            self._check_missed()
            time.sleep(30)

    def reschedule(self, config: DigestConfig) -> None:
        self._config = config
        self._schedule_job()
        logger.info(f"Rescheduled digests to {', '.join(config.digest.schedule)}")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)


def _save_last_run(tz: ZoneInfo | None = None) -> None:
    try:
        now = datetime.now(tz or ZoneInfo("UTC"))
        LAST_RUN_FILE.write_text(now.isoformat())
    except OSError:
        logger.warning("Could not save last run timestamp")


def _load_last_run() -> datetime | None:
    try:
        dt = datetime.fromisoformat(LAST_RUN_FILE.read_text().strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt
    except (OSError, ValueError):
        return None
