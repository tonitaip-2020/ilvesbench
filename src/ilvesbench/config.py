from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib


DEFAULT_LLM_MODEL = "llama3.2-3b"


@dataclass(slots=True)
class LLMConfig:
    backend: str = "aviary"
    base_url: str = ""
    api_key: str | None = None
    api_key_file: str | None = None
    model: str = DEFAULT_LLM_MODEL
    timeout_seconds: int = 180


@dataclass(slots=True)
class PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "postgres"
    password: str = "postgres"
    original_database: str = "postgres"
    new_database: str = "postgres_new"
    admin_database: str = "postgres"
    schemas: list[str] = field(default_factory=lambda: ["public"])
    connect_timeout_seconds: int = 10


@dataclass(slots=True)
class LogConfig:
    path: str = "data/postgresql.log"
    max_lines: int = 5000


@dataclass(slots=True)
class WorkloadConfig:
    path: str | None = None


@dataclass(slots=True)
class DockerConfig:
    postgres_container_name: str | None = "ilvesbench-postgres"
    prefer_container_snapshot: bool = True


@dataclass(slots=True)
class PgBenchConfig:
    enabled: bool = False
    command: str = "pgbench"
    duration_seconds: int = 30
    clients: int = 4
    jobs: int = 1
    transactions: int | None = None


@dataclass(slots=True)
class EnergyConfig:
    enabled: bool = True
    estimated_cpu_watts: float | None = None
    estimated_watts_per_cpu: float = 12.0
    co2_grams_per_kwh: float = 110.0


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: str = "data/ilvesbench_runs.sqlite3"
    artifact_dir: str = "data/artifacts"


@dataclass(slots=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(slots=True)
class IlvesBenchConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    logs: LogConfig = field(default_factory=LogConfig)
    workload: WorkloadConfig = field(default_factory=WorkloadConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    pgbench: PgBenchConfig = field(default_factory=PgBenchConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    config_path: str = ""

    @classmethod
    def from_toml(cls, path: str | Path) -> "IlvesBenchConfig":
        config_path = Path(path).expanduser().resolve()
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        llm_data = dict(data.get("llm", {}))
        llm_data.pop("model", None)
        llm_config = LLMConfig(**llm_data)
        llm_config.api_key = _resolve_llm_api_key(llm_config, config_path.parent)
        config = cls(
            llm=llm_config,
            postgres=PostgresConfig(**data.get("postgres", {})),
            logs=LogConfig(**data.get("logs", {})),
            workload=WorkloadConfig(**data.get("workload", {})),
            docker=DockerConfig(**data.get("docker", {})),
            pgbench=PgBenchConfig(**data.get("pgbench", {})),
            energy=EnergyConfig(**data.get("energy", {})),
            storage=StorageConfig(**data.get("storage", {})),
            web=WebConfig(**data.get("web", {})),
            config_path=str(config_path),
        )
        return config

    def resolve_path(self, value: str) -> Path:
        base = Path(self.config_path).parent if self.config_path else Path.cwd()
        return (base / value).resolve()


def _resolve_llm_api_key(config: LLMConfig, config_dir: Path) -> str | None:
    env_value = os.getenv("ILVESBENCH_LLM_API_KEY")
    if env_value:
        return env_value.strip()

    if config.api_key_file:
        key_path = Path(config.api_key_file).expanduser()
        if not key_path.is_absolute():
            key_path = config_dir / key_path
        if key_path.exists():
            file_value = key_path.read_text(encoding="utf-8").strip()
            if file_value:
                return file_value

    if config.api_key:
        return config.api_key.strip()

    return None
