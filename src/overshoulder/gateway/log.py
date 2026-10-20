"""Append-only JSONL action log: the audit trail and the source for tutorial rendering."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class ActionLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, **fields) -> dict:
        row = {"ts": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(row, default=str, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return row

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
