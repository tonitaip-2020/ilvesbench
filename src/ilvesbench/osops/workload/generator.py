from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re

from .aggregator import WorkloadAggregator, WorkloadEntry
from .normalizer import SQLParameter


THRESHOLD_SCALE = 1_000_000


class PgBenchWorkloadEmitter:
    def emit(
        self,
        output_dir: str | Path,
        workloads: dict[str, list[WorkloadEntry]],
        *,
        host: str = "HOST",
        port: int | str = "PORT",
        user: str = "USER",
        duration_seconds: int = 60,
        clients: int = 16,
        jobs: int = 4,
    ) -> dict[str, dict]:
        root = Path(output_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, dict] = {}
        aggregator = WorkloadAggregator()
        for database_name, entries in workloads.items():
            database_dir = root / _safe_path_name(database_name)
            queries_dir = database_dir / "queries"
            queries_dir.mkdir(parents=True, exist_ok=True)
            total_count = sum(entry.count for entry in entries)
            manifest_queries = []
            combined_blocks = [f"\\set r random(1, {THRESHOLD_SCALE})"]
            cumulative = 0
            for index, entry in enumerate(entries, start=1):
                query_id = f"q{index:04d}"
                pgbench_sql = self._pgbench_sql(entry, query_id=query_id)
                query_path = queries_dir / f"{query_id}.sql"
                query_path.write_text(pgbench_sql, encoding="utf-8")
                manifest_queries.append(
                    aggregator.manifest_entry(entry, query_id, f"queries/{query_id}.sql")
                )
                cumulative = THRESHOLD_SCALE if index == len(entries) else cumulative + round(entry.ratio * THRESHOLD_SCALE)
                keyword = "\\if" if index == 1 else "\\elif"
                combined_blocks.append(
                    "\n".join(
                        [
                            f"{keyword} :r <= {cumulative}",
                            self._indent(pgbench_sql.strip()),
                        ]
                    )
                )
            if entries:
                combined_blocks.append("\\endif")
            workload_sql = "\n".join(combined_blocks).strip() + "\n"
            (database_dir / "workload.sql").write_text(workload_sql, encoding="utf-8")
            command = (
                f"pgbench -h {host} -p {port} -U {user} -d {database_name} "
                f"-f {database_dir / 'workload.sql'} -T {duration_seconds} -c {clients} -j {jobs}"
            )
            manifest = {
                "database": database_name,
                "total_observed_queries": total_count,
                "threshold_scale": THRESHOLD_SCALE,
                "queries": manifest_queries,
                "combined_pgbench_file": "workload.sql",
                "suggested_pgbench_command": command,
            }
            (database_dir / "workload.json").write_text(
                json.dumps(manifest, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            (database_dir / "README.md").write_text(
                self._readme(database_name, manifest),
                encoding="utf-8",
            )
            outputs[database_name] = {
                "directory": str(database_dir),
                "manifest_path": str(database_dir / "workload.json"),
                "combined_workload_path": str(database_dir / "workload.sql"),
                "query_count": len(entries),
                "total_observed_queries": total_count,
                "suggested_pgbench_command": command,
            }
        return outputs

    def _pgbench_sql(self, entry: WorkloadEntry, *, query_id: str) -> str:
        lines = [
            f"-- {query_id} | observed {entry.count} | ratio {entry.ratio:.6f}",
            f"-- fingerprint {entry.fingerprint}",
        ]
        for parameter in entry.parameters:
            lines.append(self._parameter_generator(parameter, query_id=query_id))
        lines.append(self._replace_params(entry.normalized_sql, entry.parameters, query_id=query_id))
        return "\n".join(lines).strip() + "\n"

    def _parameter_generator(self, parameter: SQLParameter, *, query_id: str) -> str:
        variable = self._variable_name(parameter, query_id=query_id)
        examples = [example for example in parameter.examples if example]
        inferred_type = parameter.inferred_type
        if inferred_type == "integer":
            return f"\\set {variable} random(1, 1000000)"
        if inferred_type == "float":
            return f"\\set {variable} random(1, 1000000)"
        if inferred_type == "boolean":
            return f"\\set {variable} random(0, 1)"
        if inferred_type in {"string", "datetime", "json_or_array"}:
            value = _sql_quote(examples[0] if examples else "sample")
            return f"\\set {variable} {value}"
        return f"\\set {variable} 1"

    def _replace_params(self, sql: str, parameters: list[SQLParameter], *, query_id: str) -> str:
        result = sql
        for parameter in sorted(parameters, key=lambda item: item.position, reverse=True):
            variable = self._variable_name(parameter, query_id=query_id)
            replacement = f":{variable}"
            if parameter.inferred_type == "boolean":
                replacement = f"(:{variable})::boolean"
            result = re.sub(rf"\${parameter.position}(?!\d)", replacement, result)
        return result.rstrip(";") + ";"

    def _variable_name(self, parameter: SQLParameter, *, query_id: str) -> str:
        return f"{query_id}_p{parameter.position}"

    def _indent(self, text: str) -> str:
        return "\n".join(f"  {line}" if line.strip() else line for line in text.splitlines())

    def _readme(self, database_name: str, manifest: dict) -> str:
        return "\n".join(
            [
                f"# Workload for {database_name}",
                "",
                f"Observed queries: {manifest['total_observed_queries']}",
                f"Query templates: {len(manifest['queries'])}",
                "",
                "Run with:",
                "",
                "```bash",
                manifest["suggested_pgbench_command"],
                "```",
                "",
            ]
        )


def _safe_path_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return safe or "unknown"


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
