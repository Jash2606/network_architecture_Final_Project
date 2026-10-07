"""The helpers beside the programs: the spec checker, the measurements, the demo."""
import os
import socket
import subprocess
import sys
import threading
import unittest

from tests.test_bserve_bcurl import ROOT, BServeCase
from tools.check_server import CHECKS, run_checks
from tools.measure import size_rows


class CheckServerTest(BServeCase):
    def test_our_server_passes_every_check(self):
        host, port = self.base.split(":")
        results = run_checks(host, int(port))
        self.assertEqual([r.name for r in results if not r.passed], [])
        self.assertEqual(len(results), len(CHECKS))

    def test_a_server_that_hangs_up_fails_every_check_without_crashing(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def hang_up():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.close()

        threading.Thread(target=hang_up, daemon=True).start()
        try:
            results = run_checks("127.0.0.1", port)
        finally:
            listener.close()
        self.assertTrue(all(not r.passed and r.problem for r in results))


class MeasureTest(unittest.TestCase):
    def test_frames_are_smaller_than_text(self):
        for label, frames, text in size_rows():
            with self.subTest(label):
                self.assertLess(frames, text)


class DemoTest(unittest.TestCase):
    def test_demo_passes_end_to_end(self):
        run = subprocess.run([sys.executable, os.path.join(ROOT, "demo.py")],
                             capture_output=True, timeout=180)
        self.assertEqual(run.returncode, 0, run.stdout.decode(errors="replace")[-2000:])
        self.assertIn(b"Everything passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
