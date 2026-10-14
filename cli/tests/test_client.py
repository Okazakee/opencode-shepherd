import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocshepherd import client as clientmod


ENV_KEYS = ("OCSHEPHERD_PASSWORD", "OPENCODE_SERVER_PASSWORD")


class PasswordDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explicit_env_wins(self):
        os.environ["OCSHEPHERD_PASSWORD"] = "a"
        os.environ["OPENCODE_SERVER_PASSWORD"] = "b"
        self.assertEqual(clientmod.discover_password(), "a")

    def test_opencode_env_fallback(self):
        os.environ["OPENCODE_SERVER_PASSWORD"] = "b"
        self.assertEqual(clientmod.discover_password(), "b")

    def test_none_without_env(self):
        self.assertIsNone(clientmod.discover_password())

    def test_never_reads_opencode_credential_store(self):
        """Regression: the opencode background service's own service.json
        (with its generated password) must never be read as a credential."""
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / ".config" / "opencode" / "service.json"
            store.parent.mkdir(parents=True)
            store.write_text('{"password": "service-generated"}')
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                self.assertIsNone(clientmod.discover_password())
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
