"""Strict, streaming CSV validation. No session is changed during parsing."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from threading import Event

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot


class ImportCancelled(Exception):
    pass


def read_raw_csv(path: str | Path, cancel: Event | None = None) -> list[dict]:
    # Bound memory use rather than letting an untrusted file exhaust the process.
    max_rows = 2_000_000
    rows: list[dict] = []
    previous_t = -1
    if Path(path).stat().st_size > 256 * 1024 * 1024:
        raise ValueError("File exceeds 256 MiB")
    with Path(path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        header = next(reader, None)
        if header not in (["x", "y", "time"], ["x", "y", "time", "segment_start"]):
            raise ValueError("Expected x,y,time columns")
        extended = len(header) == 4
        for fields in reader:
            if cancel is not None and cancel.is_set():
                raise ImportCancelled()
            if not fields:
                continue
            if len(rows) >= max_rows or len(fields) != len(header):
                raise ValueError("Too many rows or incorrect column count")
            x, y, timestamp = (value.strip() for value in fields[:3])
            if not timestamp.isascii() or not timestamp.isdecimal():
                raise ValueError("Invalid elapsed milliseconds")
            t = int(timestamp)
            if t < previous_t or t > 2**53 - 1:
                raise ValueError("Invalid timeline")
            start = extended and fields[3].strip() == "1"
            if extended and fields[3].strip() not in ("", "0", "1"):
                raise ValueError("Invalid segment marker")
            if bool(x) != bool(y):
                raise ValueError("Both coordinates are required")
            if x:
                # Exported coordinates are integral global desktop pixels.
                if not re.fullmatch(r"[+-]?[0-9]+", x) or not re.fullmatch(r"[+-]?[0-9]+", y):
                    raise ValueError("Invalid coordinates")
                px, py = int(x), int(y)
                if not (-2**31 <= px < 2**31 and -2**31 <= py < 2**31):
                    raise ValueError("Coordinates out of range")
            else:
                px = py = None
            row = {"x": px, "y": py, "t": t}
            if start or not rows:
                row["segment_start"] = True
            rows.append(row)
            previous_t = t
    if not rows or not any(row["x"] is not None for row in rows):
        raise ValueError("No mouse positions")
    return rows


class RawImportWorker(QObject):
    finished = pyqtSignal(object, str)

    def __init__(self, path: str, cancel: Event) -> None:
        super().__init__()
        self.path = path
        self.cancel = cancel

    @pyqtSlot()
    def run(self) -> None:
        try:
            rows = read_raw_csv(self.path, self.cancel)
        except ImportCancelled:
            self.finished.emit(None, "cancelled")
        except (OSError, ValueError, csv.Error, UnicodeError, MemoryError):
            self.finished.emit(None, "invalid")
        else:
            self.finished.emit(rows, "")
