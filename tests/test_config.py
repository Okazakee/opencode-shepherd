import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocshepherd import config as cfgmod


class ConfigTests(unittest.TestCase):
    def test_coerce(self):
        self.assertIs(cfgmod.coerce("true"), True)
        self.assertIs(cfgmod.coerce("false"), False)
        self.assertIsNone(cfgmod.coerce("null"))
        self.assertIsNone(cfgmod.coerce("none"))
        self.assertEqual(cfgmod.coerce("30"), 30)
        self.assertEqual(cfgmod.coerce("2.5"), 2.5)
        self.assertEqual(cfgmod.coerce("hello world"), "hello world")

    def test_roundtrip_with_xdg_override(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = td
            try:
                cfg = cfgmod.load_config()
                self.assertTrue(cfg["enabled"])
                self.assertIsNone(cfg["remind_after_minutes"])
                cfg["remind_after_minutes"] = 30
                cfgmod.save_config(cfg)
                again = cfgmod.load_config()
                self.assertEqual(again["remind_after_minutes"], 30)
                self.assertTrue(str(cfgmod.config_path()).startswith(td))
            finally:
                if old is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = old


if __name__ == "__main__":
    unittest.main()
