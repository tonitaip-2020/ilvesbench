import unittest


class RuntimeDependencyTests(unittest.TestCase):
    def test_pglast_is_available_for_workload_normalization(self) -> None:
        try:
            import pglast  # noqa: F401
        except ModuleNotFoundError as exc:
            raise AssertionError(
                "pglast is required for PostgreSQL log workload normalization. "
                "Install project dependencies with `python -m pip install -e .` "
                "inside the active IlvesBench virtual environment, then run tests "
                "with `.venv/bin/python -m unittest discover -s tests`."
            ) from exc


if __name__ == "__main__":
    unittest.main()
