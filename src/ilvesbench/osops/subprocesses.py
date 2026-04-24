from __future__ import annotations

import os
import shutil
import subprocess


class SubprocessRunner:
    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def run(
        self,
        args: list[str],
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **(env or {})},
        )
