from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.config import IlvesBenchConfig, PostgresConfConfig
from ilvesbench.osops.service import OSOpsService


class OSOpsConfigTests(unittest.TestCase):
    def test_postgresql_conf_can_be_loaded_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = IlvesBenchConfig(
                postgresql_conf=PostgresConfConfig(path="postgresql.conf"),
                config_path=str(root / "config.toml"),
            )
            service = OSOpsService(config)

            saved = service.save_postgresql_conf("shared_buffers = '1GB'\n")
            loaded = service.postgresql_conf_status()

        self.assertEqual(saved["status"], "ok")
        self.assertEqual(loaded["status"], "ok")
        self.assertEqual(loaded["content"], "shared_buffers = '1GB'\n")


if __name__ == "__main__":
    unittest.main()
