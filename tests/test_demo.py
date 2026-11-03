"""The README's recording shows what cronward prints now."""

import importlib.util
import unittest
from pathlib import Path

MAKE = Path(__file__).resolve().parent.parent / "docs" / "demo" / "make.py"


class DemoTest(unittest.TestCase):
    def test_the_recording_matches_what_cronward_prints(self):
        spec = importlib.util.spec_from_file_location("demo_make", MAKE)
        make = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make)
        self.assertEqual(make.SVG.read_text(), make.svg(make.session()),
                         "docs/demo/demo.svg is out of date: run python3 docs/demo/make.py")
