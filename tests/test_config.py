from pathlib import Path
import os
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ilvesbench.config import DEFAULT_LLM_MODEL, IlvesBenchConfig


class ConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("ILVESBENCH_LLM_API_KEY", None)

    def test_llm_api_key_file_is_resolved_relative_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "secrets").mkdir()
            (root / "secrets" / "llm_key").write_text("file-key\n", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                """
                [llm]
                api_key = "literal-key"
                api_key_file = "secrets/llm_key"
                """,
                encoding="utf-8",
            )

            config = IlvesBenchConfig.from_toml(config_path)

            self.assertEqual(config.llm.api_key, "file-key")

    def test_llm_api_key_env_var_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "secrets").mkdir()
            (root / "secrets" / "llm_key").write_text("file-key\n", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                """
                [llm]
                api_key = "literal-key"
                api_key_file = "secrets/llm_key"
                """,
                encoding="utf-8",
            )
            os.environ["ILVESBENCH_LLM_API_KEY"] = "env-key"

            config = IlvesBenchConfig.from_toml(config_path)

            self.assertEqual(config.llm.api_key, "env-key")

    def test_missing_llm_api_key_file_falls_back_to_literal_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.toml"
            config_path.write_text(
                """
                [llm]
                api_key = "literal-key"
                api_key_file = "secrets/missing_key"
                """,
                encoding="utf-8",
            )

            config = IlvesBenchConfig.from_toml(config_path)

            self.assertEqual(config.llm.api_key, "literal-key")

    def test_llm_model_is_defined_only_in_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.toml"
            config_path.write_text(
                """
                [llm]
                model = "phi4-14b"
                """,
                encoding="utf-8",
            )

            config = IlvesBenchConfig.from_toml(config_path)

            self.assertEqual(config.llm.model, DEFAULT_LLM_MODEL)


if __name__ == "__main__":
    unittest.main()
