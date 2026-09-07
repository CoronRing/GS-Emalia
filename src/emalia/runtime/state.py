"""Durable state for a listener: what has been handled, and what it did.

Both files live under the config's `state_dir`. Neither holds a secret: the
seen-set holds Message-IDs, and the audit log holds addresses, subjects and
outcomes but never bodies or credentials.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["SeenStore", "AuditLog"]

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, payload: str) -> None:
    """Write a file so a crash mid-write cannot truncate the old one.

    Args:
        path: The destination file.
        payload: The text to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed then os.replace'd
        "w",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        encoding="utf-8",
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


class SeenStore:
    """The set of Message-IDs already handled.

    Without this, a crash between answering an email and marking it read makes
    the listener answer it again on restart. The store is bounded: only the
    most recent `capacity` IDs are kept, which is plenty to cover the window
    where a duplicate is possible and keeps the file from growing forever.

    Attributes:
        path: Where the set is persisted.
        capacity: How many IDs to retain.
    """

    def __init__(self, path: Path, *, capacity: int = 2000) -> None:
        """
        Args:
            path: The JSON file backing the store.
            capacity: How many IDs to retain, oldest dropped first.
        """
        self.path = Path(path)
        self.capacity = capacity
        self._order: deque[str] = deque(maxlen=capacity)
        self._members: set[str] = set()
        self.load()

    def load(self) -> None:
        """Read the store from disk, starting empty if it is missing or bad."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ids = data["seen"] if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            logger.warning("Could not read %s; starting with an empty seen set.", self.path)
            return
        for message_id in list(ids)[-self.capacity :]:
            self.add(str(message_id), persist=False)

    def save(self) -> None:
        """Write the store to disk atomically."""
        payload = json.dumps({"seen": list(self._order)}, indent=0)
        try:
            _atomic_write(self.path, payload)
        except OSError:
            # A listener that cannot persist its seen-set is degraded, not
            # broken: it still de-duplicates in memory for this run.
            logger.warning("Could not write %s; de-duplication is in-memory only.", self.path)

    def add(self, message_id: str, *, persist: bool = True) -> None:
        """Record a Message-ID as handled.

        Args:
            message_id: The ID to record.
            persist: Write to disk afterwards.
        """
        if message_id in self._members:
            return
        if len(self._order) == self._order.maxlen:
            self._members.discard(self._order[0])
        self._order.append(message_id)
        self._members.add(message_id)
        if persist:
            self.save()

    def __contains__(self, message_id: object) -> bool:
        return isinstance(message_id, str) and message_id in self._members

    def __len__(self) -> int:
        return len(self._members)

    def extend(self, message_ids: Iterable[str]) -> None:
        """Record several IDs, writing to disk once.

        Args:
            message_ids: The IDs to record.
        """
        for message_id in message_ids:
            self.add(message_id, persist=False)
        self.save()


class AuditLog:
    """An append-only JSONL record of what the listener did.

    One line per message considered, whether or not it was answered. This is
    the artefact you read after something goes wrong, so it records refusals
    as prominently as successes.

    Attributes:
        path: The JSONL file written to.
    """

    def __init__(self, path: Path) -> None:
        """
        Args:
            path: Where to append records.
        """
        self.path = Path(path)

    def record(
        self,
        event: str,
        *,
        message_id: str | None = None,
        sender: str | None = None,
        subject: str | None = None,
        outcome: str = "",
        detail: str = "",
        **extra: Any,
    ) -> None:
        """Append one record.

        Args:
            event: What happened, e.g. ``handled``, ``ignored``, ``failed``.
            message_id: The message the record is about.
            sender: The sender address.
            subject: The subject line.
            outcome: A short result string.
            detail: Any further explanation.
            **extra: Additional JSON-serialisable fields.
        """
        record = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "outcome": outcome,
            "detail": detail,
            **extra,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.warning("Could not append to the audit log at %s", self.path, exc_info=True)

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        """Read the most recent records.

        Args:
            count: How many records to return.

        Returns:
            The records, oldest first. Empty if the log does not exist.
        """
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-count:]
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
