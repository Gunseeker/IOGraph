"""Bounded recording workload; writes metrics only to the requested output file.

Run from the root: python scripts/profile_memory.py --output build/memory-audit/after.json
No real recording, AppData, or system setting is read or changed.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
import tracemalloc
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PyQt6.QtWidgets import QApplication
from iograph.tracker import TrackCanvas
from iograph.core.raw_import import read_raw_csv
from iograph.core.session_storage import SessionStorage


def process_memory():
    if sys.platform != "win32":
        return {}
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = wintypes.HANDLE
    handle = process()
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not get_memory(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    gui = ctypes.windll.user32.GetGuiResources
    gui.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return {"private_bytes": counters.PrivateUsage, "working_set_bytes": counters.WorkingSetSize,
            "gdi_objects": gui(handle, 0), "user_objects": gui(handle, 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200000)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-only", action="store_true", help="Simulate up to one day without CSV import")
    args = parser.parse_args()
    if not 1 <= args.samples <= 2_700_000:
        parser.error("Use 1..2700000 samples")
    app = QApplication.instance() or QApplication([])
    canvas = TrackCanvas()
    canvas.resize(400, 200)
    canvas._ensure_buffers()
    tracemalloc.start()
    metrics = {"samples": args.samples, "simulated_recording_hours": args.samples * 33 / 3600000,
               "python": sys.version, "phases": []}

    def measure(name, operation):
        gc.collect()
        tracemalloc.reset_peak()
        start = perf_counter()
        result = operation()
        current, peak = tracemalloc.get_traced_memory()
        metrics["phases"].append({"phase": name, "seconds": perf_counter() - start,
                                  "python_bytes": current, "python_peak_bytes": peak,
                                  **process_memory()})
        return result

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def record():
            for i in range(args.samples):
                canvas._raw_samples.append((float(i % 1000), float((i * 7) % 1000), i * 33))
            canvas._segment_starts.add(0)
            canvas._accumulated_ms = canvas._last_elapsed_ms = (args.samples - 1) * 33

        measure("record", record)
        snapshots = measure("two_render_snapshots", lambda: [canvas.snapshot_preview_render_state() for _ in range(2)])
        if args.record_only:
            def verify():
                for i, (x, y, t) in enumerate(canvas._raw_samples):
                    if (x, y, t) != (float(i % 1000), float((i * 7) % 1000), i * 33):
                        raise AssertionError(f"Sample {i} was changed")
                return i + 1
            metrics["verified_samples"] = measure("verify_all_samples", verify)
            metrics["packed_file_bytes"] = max(0, len(canvas._raw_samples) - 1) * 25
            del snapshots
            measure("reset", canvas.reset)
            gc.collect()
            measure("collected", lambda: None)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            print(json.dumps(metrics, indent=2))
            return
        storage = SessionStorage("session.json", 3600000)
        with patch.object(storage, "session_state_path", return_value=root / "session.json"):
            def save():
                rows = canvas.iter_raw_samples() if hasattr(canvas, "iter_raw_samples") else canvas.export_raw_samples()
                state = {"raw_storage": storage.write_raw_chunks(rows)}
                storage.save_state(state)
                return state
            state = measure("save", save)
            other = TrackCanvas()
            measure("restore", lambda: other.load_raw_samples(
                storage.iter_raw_samples(state) if hasattr(storage, "iter_raw_samples") else storage.read_raw_samples(state), rebuild=False))
            other.reset()
            other.deleteLater()
        csv_path = root / "samples.csv"

        def export():
            if hasattr(canvas, "write_csv"):
                canvas.write_csv(csv_path)
            else:
                csv_path.write_text(canvas.export_csv_text(), encoding="utf-8")

        measure("csv_export", export)
        rows = measure("csv_parse", lambda: read_raw_csv(csv_path))
        measure("append", lambda: canvas.import_raw_samples(rows))
        del rows, snapshots
        gc.collect()
        measure("reset", canvas.reset)
        gc.collect()
        measure("collected", lambda: None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
