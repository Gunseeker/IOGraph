from __future__ import annotations

import gc
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import tracemalloc
import unittest
import weakref
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, QThread, QTimer
from PyQt6.QtGui import QCloseEvent, QImage
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog
from iograph.core.sample_store import SampleStore, RawRecording
from iograph.core.session_storage import SessionStorage
from iograph.main import MainWindow, PreviewRenderWorker
from iograph.services.settings import AppSettings
from iograph.tracker import TrackCanvas

APP = QApplication.instance() or QApplication([])


def drain_deletes():
    # Destroying a widget can itself defer deletion of PyQt signal proxies.
    for _ in range(2):
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()


class SampleStoreTests(unittest.TestCase):
    def test_exact_values_and_idle_tail(self):
        samples = [(-2147483648.0, 2147483647.0, 0), (1.25, -3.5, 2**53 + 17),
                   (None, None, 2**53 + 19)]
        store = SampleStore()
        store.extend(samples)
        self.assertEqual(list(store), samples)
        snapshot = store.snapshot()
        store[-1] = (None, None, 2**53 + 200)
        self.assertEqual(list(snapshot), samples)
        self.assertEqual(store[-1][2], 2**53 + 200)

    def test_extend_packed_chunks_and_self(self):
        original = [(float(i), -float(i), i * 33) for i in range(20000)]
        source = SampleStore()
        source.extend(original)
        dest = SampleStore()
        dest.append((None, None, 0))
        dest.extend(source)
        dest.extend(source.snapshot())
        self.assertEqual(list(dest), [(None, None, 0)] + original + original)
        source.extend(source)
        self.assertEqual(list(source), original + original)

    def test_snapshot_thread_safety_and_lifetime(self):
        store = SampleStore()
        expected = [(float(i), -float(i), i * 33) for i in range(20000)]
        store.extend(expected)
        snapshot = store.snapshot()
        owner_ref = weakref.ref(store)
        file_ref = weakref.ref(store._file)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reads = [pool.submit(list, snapshot) for _ in range(2)]
            for i in range(1000):
                store.append((1.0, 2.0, 660000 + i))
            for result in reads:
                self.assertEqual(result.result(), expected)
        del store
        gc.collect()
        self.assertIsNotNone(owner_ref())
        self.assertEqual(list(snapshot), expected)
        del snapshot
        gc.collect()
        self.assertIsNone(owner_ref())
        self.assertIsNone(file_ref())

    def test_idle_updates_do_not_accumulate_records(self):
        store = SampleStore()
        store.append((None, None, 0))
        for i in range(100000):
            store[-1] = (None, None, i * 33)
        self.assertEqual(len(store), 1)
        self.assertIsNone(store._file)
        self.assertEqual(store[-1], (None, None, 99999 * 33))

    def test_bounded_python_allocations_and_repeated_clear(self):
        tracemalloc.start()
        try:
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(3):
                store = SampleStore()
                for i in range(100000):
                    store.append((float(i), float(i), i * 33))
                snapshot = store.snapshot()
                self.assertLess(tracemalloc.get_traced_memory()[0] - before, 500000)
                self.assertEqual(sum(1 for _ in snapshot), 100000)
                del store, snapshot
                gc.collect()
            self.assertLess(tracemalloc.get_traced_memory()[0] - before, 500000)
        finally:
            tracemalloc.stop()

    def test_failed_append_preserves_tail(self):
        store = SampleStore()
        store.append((1.0, 2.0, 3))
        with patch("iograph.core.sample_store.TemporaryFile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.append((4.0, 5.0, 6))
        self.assertEqual(list(store), [(1.0, 2.0, 3)])


class PersistenceAndRenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = SessionStorage("session.json", 1000)
        self.path_patch = patch.object(SessionStorage, "session_state_path", return_value=self.root / "session.json")
        self.path_patch.start()
        self.canvas = TrackCanvas()
        self.canvas.resize(200, 100)

    def tearDown(self):
        self.canvas.stop_tracking()
        self.canvas.deleteLater()
        drain_deletes()
        self.path_patch.stop()
        self.temp.cleanup()

    def rows(self):
        return [{"x": i, "y": -i, "t": i * 33, "segment_start": i % 300 == 0} for i in range(2000)]

    def save(self, rows):
        state = {"raw_storage": self.storage.write_raw_chunks(rows)}
        self.storage.save_state(state)
        return state

    def test_streaming_save_restore_and_old_generations_removed(self):
        expected = self.rows()
        first = self.save(iter(expected))
        old_names = {c["file"] for c in first["raw_storage"]["chunks"]}
        second = self.save(iter(expected))
        self.assertEqual(list(self.storage.iter_raw_samples(second)), expected)
        self.assertTrue(all(not (self.storage.raw_chunks_dir() / name).exists() for name in old_names))
        self.canvas.load_raw_samples(self.storage.iter_raw_samples(second), rebuild=False)
        self.assertEqual(self.canvas.export_raw_samples(), expected)

    def test_failed_save_keeps_previous_manifest_and_chunks(self):
        expected = self.rows()
        self.save(iter(expected))
        before = self.storage.session_state_path().read_bytes()

        def broken():
            yield expected[0]
            raise OSError("disk full")

        with self.assertRaises(OSError):
            self.storage.write_raw_chunks(broken())
        self.assertEqual(self.storage.session_state_path().read_bytes(), before)
        self.assertEqual(self.storage.read_raw_samples(self.storage.load_state()), expected)
        next_state = {"raw_storage": self.storage.write_raw_chunks(iter(expected))}
        with patch("iograph.core.session_storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.storage.save_state(next_state)
        self.assertEqual(self.storage.session_state_path().read_bytes(), before)
        self.assertEqual(self.storage.read_raw_samples(self.storage.load_state()), expected)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_incomplete_restore_does_not_replace_current_data(self):
        self.canvas.load_raw_samples(self.rows(), rebuild=False)
        before = self.canvas.export_raw_samples()
        state = self.save(self.rows())
        (self.storage.raw_chunks_dir() / state["raw_storage"]["chunks"][-1]["file"]).unlink()
        with self.assertRaises(OSError):
            self.canvas.load_raw_samples(self.storage.iter_raw_samples(state), rebuild=False)
        self.assertEqual(self.canvas.export_raw_samples(), before)

    def test_import_failure_is_transactional(self):
        self.canvas.load_raw_samples(self.rows(), rebuild=False)
        before = self.canvas.export_raw_samples()

        def broken():
            yield {"x": -99, "y": 50, "t": 0}
            raise OSError("disk full")

        with self.assertRaises(OSError):
            self.canvas.import_raw_samples(broken(), replace=True)
        self.assertEqual(self.canvas.export_raw_samples(), before)

    def test_list_and_packed_rendering_are_pixel_identical(self):
        samples = [(10.0, 50.0, 0), (20.0, 50.0, 33), (None, None, 3300),
                   (90.0, 80.0, 3333), (None, None, 10000), (180.0, 50.0, 10000),
                   (190.0, 50.0, 10033)]
        store = SampleStore()
        store.extend(samples)
        for colorful in (False, True):
            for ignore_stops in (False, True):
                images = []
                for rows in (samples, store.snapshot()):
                    image = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(0)
                    TrackCanvas._render_raw_on_image(image, rows, colorful, ignore_stops,
                        (0, 0, 200, 100), (200, 100), 1.0, 33, segment_starts={0, 5})
                    images.append(image)
                self.assertEqual(images[0], images[1])

    def test_streamed_csv_matches_compatibility_export(self):
        self.canvas.load_raw_samples(self.rows(), rebuild=False)
        path = self.root / "recording.csv"
        self.canvas.write_csv(path)
        self.assertEqual(path.read_text(), self.canvas.export_csv_text())

    def test_storage_failure_pauses_timer_without_losing_data(self):
        self.canvas.load_raw_samples(self.rows(), rebuild=False)
        before = self.canvas.export_raw_samples()
        errors = []
        self.canvas.recording_storage_failed.connect(lambda: errors.append(True))
        self.canvas.start_tracking()
        with patch.object(self.canvas._raw_samples, "append", side_effect=OSError("disk full")), \
             patch("iograph.tracker.QCursor.pos", return_value=QPoint(50, 50)):
            self.canvas._on_tick()
        self.assertFalse(self.canvas.is_tracking())
        self.assertFalse(self.canvas._timer.isActive())
        self.assertEqual(self.canvas.export_raw_samples(), before)
        self.assertEqual(errors, [True])

    def test_preview_mailbox_is_bounded_and_releases_state(self):
        self.canvas.load_raw_samples(self.rows(), rebuild=False)
        state = self.canvas.snapshot_preview_render_state()
        owner = weakref.ref(state["raw_samples"]._owner)
        worker = PreviewRenderWorker(1, state)
        notifications = []
        worker.progress.connect(lambda: notifications.append(True))
        for _ in range(100):
            worker.publish_progress(QImage(200, 100, QImage.Format.Format_ARGB32))
        self.assertEqual(len(notifications), 1)
        latest = worker.take_progress()
        self.assertIsNotNone(latest)
        self.assertIsNone(worker.take_progress())
        worker.run()
        self.assertEqual(worker._state, {})
        self.assertIsNone(worker.take_progress())
        self.canvas.reset()
        del state, latest
        gc.collect()
        self.assertIsNone(owner())
        worker.deleteLater()

    def test_repeated_imports_do_not_retain_dialogs_or_threads(self):
        settings = AppSettings("IOGraphTests", "Memory")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        path = self.root / "import.csv"
        path.write_text("x,y,time\n1,2,0\n5,6,33\n")
        with patch("iograph.main.AppSettings", return_value=settings), \
             patch.object(MainWindow, "_set_tracking"):
            window = MainWindow()
        while window._preview_render_thread is not None:
            APP.processEvents()

        def choose_append(box):
            box.buttons()[0].click()
            return 0

        with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(path), "")), \
             patch.object(QMessageBox, "exec", choose_append):
            for _ in range(25):
                window._load_csv()
                while window._preview_render_thread is not None:
                    APP.processEvents()
                drain_deletes()
        self.assertEqual(len(window._canvas._raw_samples), 50)
        self.assertEqual(len(window.findChildren(QProgressDialog)), 0)
        self.assertEqual(len(window.findChildren(QMessageBox)), 0)
        self.assertEqual(len(window.findChildren(QThread)), 0)
        if getattr(window, "_tray_icon", None):
            window._tray_icon.hide()
        window_ref = weakref.ref(window)
        window.deleteLater()
        del window
        drain_deletes()
        self.assertIsNone(window_ref())

    def test_repeated_desktop_refresh_is_coalesced(self):
        from iograph.ui.desktop_snapshot_controller import DesktopSnapshotController
        window = Mock()
        window.isVisible.return_value = True
        window.isMinimized.return_value = False
        window.hide.side_effect = lambda: setattr(window.isVisible, "return_value", False)
        capture = Mock(return_value=True)
        controller = DesktopSnapshotController(window, capture_desktop_background=capture, on_status=Mock())
        callbacks = []
        with patch.object(QTimer, "singleShot", side_effect=lambda delay, callback: callbacks.append(callback)):
            for _ in range(100):
                controller.refresh()
            self.assertEqual(len(callbacks), 1)
            while callbacks:
                callbacks.pop(0)()
        capture.assert_called_once()
        window.showNormal.assert_called_once()
        self.assertFalse(controller._capturing)

    def test_shutdown_cancels_preview_and_releases_worker_snapshot(self):
        settings = AppSettings("IOGraphTests", "Shutdown")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        with patch("iograph.main.AppSettings", return_value=settings), patch.object(MainWindow, "_set_tracking"):
            window = MainWindow()
        window._canvas.load_raw_samples(self.rows(), rebuild=False)
        window._request_preview_rerender()
        thread = window._preview_render_thread
        worker = window._preview_render_worker
        window._finish_render_workers()
        self.assertFalse(thread.isRunning())
        self.assertEqual(worker._state, {})
        self.assertIsNone(worker.take_progress())
        drain_deletes()
        if getattr(window, "_tray_icon", None):
            window._tray_icon.hide()
        window.deleteLater()
        del window
        drain_deletes()

    def test_failed_restore_blocks_overwriting_recoverable_files(self):
        state = self.save(self.rows())
        (self.storage.raw_chunks_dir() / state["raw_storage"]["chunks"][-1]["file"]).unlink()
        before = self.storage.session_state_path().read_bytes()
        settings = AppSettings("IOGraphTests", "RestoreFailure")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        with patch("iograph.main.AppSettings", return_value=settings), \
             patch.object(MainWindow, "_set_tracking"), patch.object(QMessageBox, "warning") as warning:
            window = MainWindow()
            self.assertTrue(window._session_restore_failed)
            window._save_session_state()
            self.assertEqual(self.storage.session_state_path().read_bytes(), before)
            warning.assert_called_once()
        if getattr(window, "_tray_icon", None):
            window._tray_icon.hide()
        window.deleteLater()
        del window
        drain_deletes()

    def test_corrupt_manifest_and_truncated_chunk_are_not_silently_discarded(self):
        path = self.storage.session_state_path()
        path.write_text("{invalid")
        with self.assertRaises(ValueError):
            self.storage.load_state()
        state = self.save(self.rows())
        chunk = self.storage.raw_chunks_dir() / state["raw_storage"]["chunks"][0]["file"]
        chunk.write_text(chunk.read_text().splitlines()[0] + "\n")
        with self.assertRaises(ValueError):
            list(self.storage.iter_raw_samples(state))

    def test_failed_save_keeps_window_and_recording_open(self):
        settings = AppSettings("IOGraphTests", "SaveFailure")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        with patch("iograph.main.AppSettings", return_value=settings), patch.object(MainWindow, "_set_tracking"):
            window = MainWindow()
        window._canvas.load_raw_samples(self.rows(), rebuild=False)
        expected = window._canvas.export_raw_samples()
        window._force_quit_requested = True
        event = QCloseEvent()
        with patch.object(window, "_write_session_state", side_effect=OSError("disk full")), \
             patch.object(QMessageBox, "warning"):
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(window._closing)
        self.assertEqual(window._canvas.export_raw_samples(), expected)
        drain_deletes()
        if getattr(window, "_tray_icon", None):
            window._tray_icon.hide()
        window.deleteLater()
        del window
        drain_deletes()

    def test_preview_failure_does_not_repeat_allocation_on_gui_thread(self):
        settings = AppSettings("IOGraphTests", "PreviewFailure")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        with patch("iograph.main.AppSettings", return_value=settings), patch.object(MainWindow, "_set_tracking"):
            window = MainWindow()
        while window._preview_render_thread is not None:
            APP.processEvents()
        window._canvas.load_raw_samples(self.rows(), rebuild=False)
        expected = window._canvas.export_raw_samples()
        with patch.object(window._canvas, "rebuild_from_raw_samples") as rebuild:
            window._on_preview_rerender_failed("out of memory")
            rebuild.assert_not_called()
        self.assertEqual(window._canvas.export_raw_samples(), expected)
        if getattr(window, "_tray_icon", None):
            window._tray_icon.hide()
        window.deleteLater()
        del window
        drain_deletes()


if __name__ == "__main__":
    unittest.main()
