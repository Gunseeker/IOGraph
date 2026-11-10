"""Run with python -m unittest discover -s scripts -p test_raw_import.py -v."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
import unittest
from threading import Event
from unittest.mock import patch

from PyQt6.QtCore import QPoint, QSettings
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QMessageBox

from iograph.core.raw_import import ImportCancelled, read_raw_csv
from iograph.core.session_storage import SessionStorage
from iograph.core.session_controller import SessionController
from iograph.core.tracking_controller import TrackingController
from iograph.tracker import TrackCanvas

APP = QApplication.instance() or QApplication([])
APP.setApplicationName("IOGraphRawDataTests")


class RawImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.canvas = TrackCanvas()
        self.canvas.resize(400, 200)

    def tearDown(self):
        self.canvas.stop_tracking()
        self.canvas.deleteLater()
        self.temp.cleanup()

    def read(self, text):
        path = self.root / "data.csv"
        path.write_text(text, encoding="utf-8")
        return read_raw_csv(path)

    def recording(self):
        return self.read("x,y,time\n-100,20,100\n,,5000\n200,80,6000\n")

    def test_empty_and_negative_coordinates_and_idle(self):
        self.canvas.import_raw_samples(self.recording())
        self.assertEqual(self.canvas.get_elapsed_ms(), 6000)
        self.assertEqual(self.canvas._raw_samples, [(-100, 20, 100), (None, None, 5000), (200, 80, 6000)])
        self.assertEqual(self.canvas._segment_starts, {0})

    def test_append_repeatedly_and_replace(self):
        rows = self.recording()
        self.canvas.import_raw_samples(rows)
        original = self.canvas.export_raw_samples()
        self.canvas.import_raw_samples(rows)
        self.canvas.import_raw_samples(rows)
        self.assertEqual(self.canvas.export_raw_samples()[:3], original)
        self.assertEqual(self.canvas.get_elapsed_ms(), 18000)
        self.assertEqual(self.canvas._segment_starts, {0, 3, 6})
        self.assertEqual(self.canvas._raw_samples[3][2], 6100)
        self.canvas.import_raw_samples(rows, replace=True)
        self.assertEqual(self.canvas.get_elapsed_ms(), 6000)
        self.assertEqual(self.canvas.export_raw_samples(), original)

    def test_export_import_round_trip(self):
        self.canvas.import_raw_samples(self.recording())
        self.canvas.import_raw_samples(self.recording())
        self.canvas.restore_elapsed_ms(12345)
        text = self.canvas.export_csv_text()
        other = TrackCanvas()
        other.import_raw_samples(self.read(text))
        self.assertEqual(other.get_elapsed_ms(), 12345)
        self.assertEqual(other._segment_starts, self.canvas._segment_starts)
        self.assertEqual(other._raw_samples[:-1], self.canvas._raw_samples)
        self.assertEqual(other.export_csv_text(), text)
        other.deleteLater()

    def test_chunks_restore_and_duration(self):
        self.canvas.import_raw_samples(self.recording())
        self.canvas.import_raw_samples(self.recording())
        storage = SessionStorage("session.json", 3000)
        with patch.object(storage, "session_state_path", return_value=self.root / "session.json"):
            state = {"raw_storage": storage.write_raw_chunks(self.canvas.export_raw_samples()), "elapsed_ms": 12345}
            storage.save_state(state)
            loaded = storage.load_state()
            other = TrackCanvas()
            other.load_raw_samples(storage.read_raw_samples(loaded))
            other.restore_elapsed_ms(loaded["elapsed_ms"])
            self.assertEqual(other.export_raw_samples(), self.canvas.export_raw_samples())
            self.assertEqual(other.get_elapsed_ms(), 12345)
            other.deleteLater()

    def test_resuming_live_tracking(self):
        self.canvas.import_raw_samples(self.recording())
        with patch("iograph.tracker.QCursor.pos", return_value=QPoint(800, 100)):
            self.canvas.start_tracking()
            self.canvas._on_tick()
            self.assertTrue(self.canvas.is_tracking())
            self.assertIn(3, self.canvas._segment_starts)
            self.assertGreaterEqual(self.canvas._raw_samples[-1][2], 6000)
            self.canvas.stop_tracking()
            before = self.canvas.get_elapsed_ms()
            self.canvas.start_tracking()
            self.canvas._on_tick()
            self.assertGreaterEqual(self.canvas._raw_samples[-1][2], before)

    def test_import_while_tracking(self):
        self.canvas.import_raw_samples(self.recording())
        self.canvas.start_tracking()
        self.canvas._on_tick()
        old = list(self.canvas._raw_samples)
        self.canvas.import_raw_samples(self.recording())
        self.assertTrue(self.canvas.is_tracking())
        self.assertEqual(self.canvas._raw_samples[:len(old)], old)
        self.canvas._on_tick()
        self.assertGreaterEqual(self.canvas._raw_samples[-1][2], 12000)

    def test_invalid_input_keeps_session(self):
        self.canvas.import_raw_samples(self.recording())
        before = self.canvas.export_raw_samples()
        invalid = ("", "x,y,z\n1,2,3\n", "x,y,time\n", "x,y,time\n1,,3\n",
                   "x,y,time\n1,2,-1\n", "x,y,time\n1,2,10\n1,2,9\n",
                   "x,y,time\nnan,2,1\n", "x,y,time\ninf,2,1\n", "x,y,time\n1,2,1.1\n",
                   "x,y,time\n1_000,2,1\n", "x,y,time\n1,2,3,4\n",
                   "x,y,time\n,,3\n", 'x,y,time\n"1,2,3\n',
                   "x,y,time,segment_start\n1,2,3,bad\n", "x,y,time\n2147483648,0,0\n")
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises((ValueError, __import__("csv").Error)):
                    self.read(text)
                self.assertEqual(before, self.canvas.export_raw_samples())
        with self.assertRaises(OSError):
            read_raw_csv(self.root / "missing.csv")

    def test_cancel_and_bom(self):
        path = self.root / "bom.csv"
        path.write_text("x,y,time\n1,2,3\n", encoding="utf-8-sig")
        self.assertEqual(len(read_raw_csv(path)), 1)
        cancel = Event()
        cancel.set()
        with self.assertRaises(ImportCancelled):
            read_raw_csv(path, cancel)

    def test_large_recording(self):
        path = self.root / "large.csv"
        with path.open("w") as output:
            output.write("x,y,time\n")
            for i in range(300000):
                output.write(f"{i % 100},20,{i * 33}\n")
        rows = read_raw_csv(path)
        self.assertEqual(len(rows), 300000)
        self.canvas.import_raw_samples(rows)
        self.assertEqual(len(self.canvas._raw_samples), 300000)
        self.assertEqual(self.canvas.get_elapsed_ms(), 299999 * 33)

    def test_image_export_and_visualization_settings(self):
        self.canvas.import_raw_samples(self.recording())
        self.canvas.import_raw_samples(self.recording())
        self.canvas.set_colorful_scheme(True)
        self.canvas.set_ignore_mouse_stops(True)
        self.canvas.set_use_multiple_monitors(False)
        path = self.root / "combined.png"
        self.assertTrue(self.canvas.export_png(str(path)))
        self.assertFalse(QImage(str(path)).isNull())
        self.assertEqual(self.canvas._segment_starts, {0, 3})

    def test_oversized_file_is_rejected_before_parsing(self):
        path = self.root / "large.csv"
        path.write_text("x,y,time\n1,2,3\n")
        from types import SimpleNamespace
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=257 * 1024 * 1024)):
            with self.assertRaises(ValueError):
                read_raw_csv(path)

    def render(self, rows, starts):
        image = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        TrackCanvas._render_raw_on_image(image, rows, False, False, (0, 0, 200, 100), (200, 100), 1, 33,
                                         segment_starts=starts)
        return image

    def test_no_line_or_idle_across_boundaries(self):
        image = self.render([(10, 50, 0), (20, 50, 33), (180, 50, 3600033), (190, 50, 3600066)], {0, 2})
        self.assertGreater(image.pixelColor(15, 50).alpha(), 0)
        self.assertEqual(image.pixelColor(100, 50).alpha(), 0)
        self.assertEqual(image.pixelColor(180, 51).alpha(), 0)

    def test_idle_at_end_of_segment_is_drawn(self):
        image = self.render([(50, 50, 0), (None, None, 3300), (180, 50, 3300), (190, 50, 3333)], {0, 2})
        self.assertGreater(image.pixelColor(50, 50).alpha(), 0)
        self.assertEqual(image.pixelColor(120, 50).alpha(), 0)

    def test_leading_idle_preserves_time_without_inventing_position(self):
        rows = self.read("x,y,time\n,,100\n10,50,200\n20,50,300\n")
        self.canvas.import_raw_samples(rows)
        self.assertEqual(self.canvas.get_elapsed_ms(), 300)
        self.assertGreater(self.render(self.canvas._raw_samples, {0}).pixelColor(15, 50).alpha(), 0)

    def test_imported_dates_not_inferred(self):
        controller = TrackingController(SessionController())
        controller.mark_imported_session()
        controller.set_tracking(True)
        self.assertEqual(controller.period_label(), "session.time_period")
        self.assertNotIn("From", controller.export_base_name(6000))
        controller.apply_reset(False)
        self.assertFalse(controller.has_imported_session)

    def test_ui_import_worker_and_saved_restart(self):
        from iograph.main import MainWindow
        from iograph.services.settings import AppSettings
        settings = AppSettings("IOGraphTests", "RawImport")
        settings._settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        session_path = self.root / "session.json"
        self.recording()
        with patch("iograph.main.AppSettings", return_value=settings), \
             patch.object(SessionStorage, "session_state_path", return_value=session_path):
            window = MainWindow()
            window._canvas.stop_tracking()
            window._canvas.import_raw_samples(self.recording())

            def choose_append(box):
                button = box.buttons()[0]
                button.click()
                return 0

            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(self.root / "data.csv"), "")), \
                 patch.object(QMessageBox, "exec", choose_append):
                window._load_csv()
            self.assertEqual(window._canvas.get_elapsed_ms(), 12000)
            self.assertTrue(window._tracking_controller.has_imported_session)
            before = window._canvas.export_raw_samples()
            # Cancelling the file picker leaves the entire session untouched.
            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=("", "")):
                window._load_csv()
            self.assertEqual(window._canvas.export_raw_samples(), before)

            def choose_replace(box):
                next(b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole).click()
                return 0

            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(self.root / "data.csv"), "")), \
                 patch.object(QMessageBox, "exec", choose_replace), \
                 patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                window._load_csv()
            self.assertEqual(window._canvas.export_raw_samples(), before)
            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(self.root / "data.csv"), "")), \
                 patch.object(QMessageBox, "exec", choose_replace), \
                 patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                window._load_csv()
            self.assertEqual(window._canvas.get_elapsed_ms(), 6000)
            before = window._canvas.export_raw_samples()
            (self.root / "data.csv").write_text("wrong,header\n")
            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(self.root / "data.csv"), "")), \
                 patch.object(QMessageBox, "exec", choose_append), \
                 patch.object(QMessageBox, "warning") as warning:
                window._load_csv()
            warning.assert_called_once()
            self.assertEqual(window._canvas.export_raw_samples(), before)

            from iograph.core.raw_import import RawImportWorker

            def cancelled_worker(path, cancel):
                cancel.set()
                return RawImportWorker(path, cancel)

            self.recording()
            with patch("iograph.main.QFileDialog.getOpenFileName", return_value=(str(self.root / "data.csv"), "")), \
                 patch.object(QMessageBox, "exec", choose_append), \
                 patch("iograph.main.RawImportWorker", side_effect=cancelled_worker):
                window._load_csv()
            self.assertEqual(window._canvas.export_raw_samples(), before)
            # Restore a combined recording before the restart assertion.
            window._canvas.import_raw_samples(self.recording())
            while window._preview_render_thread is not None:
                APP.processEvents()
            window._save_session_state()
            other = MainWindow()
            other._canvas.stop_tracking()
            self.assertEqual(other._canvas.export_raw_samples(), window._canvas.export_raw_samples())
            self.assertEqual(other._canvas.get_elapsed_ms(), 12000)
            self.assertTrue(other._tracking_controller.has_imported_session)
            for instance in (window, other):
                while instance._preview_render_thread is not None:
                    APP.processEvents()
                if getattr(instance, "_tray_icon", None):
                    instance._tray_icon.hide()
                instance.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
