from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
import re

from ilvesbench.models import LogSummary, QueryObservation, TransactionObservation
from ilvesbench.config import PgBenchConfig, PostgresConfig
from ilvesbench.osops.workload import WorkloadLogPipeline


STATEMENT_RE = re.compile(
    r"duration:\s+(?P<duration>[0-9.]+)\s+ms\s+(?:execute\s+[^:]+:\s+|statement:\s+)(?P<sql>.+)$"
)
PID_RE = re.compile(r"\[(?P<pid>\d+)\]")
WS_RE = re.compile(r"\s+")


class PostgresLogParser:
    def parse(
        self,
        path: str | Path,
        max_lines: int = 5000,
        *,
        output_dir: str | Path | None = None,
        postgres: PostgresConfig | None = None,
        pgbench: PgBenchConfig | None = None,
    ) -> LogSummary:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"PostgreSQL log file not found: {target}")

        transaction_lines_processed = 0
        open_transactions, finished_transactions = self._parse_transactions(target, max_lines=max_lines)
        pipeline = WorkloadLogPipeline()
        result = pipeline.process_files(
            [target],
            output_dir=output_dir,
            max_lines=max_lines,
            postgres=postgres,
            pgbench=pgbench,
        )
        top_queries = self._query_observations(result["workloads"])
        sampled_transactions = finished_transactions[:10]
        multi_statement_transactions = sum(1 for tx in finished_transactions if tx.statement_count > 1)
        return LogSummary(
            path=str(target),
            lines_processed=result["lines_processed"],
            statements_detected=result["observed_queries"],
            transactions_detected=len(finished_transactions),
            multi_statement_transactions=multi_statement_transactions,
            top_queries=top_queries,
            sampled_transactions=sampled_transactions,
            source_kind="postgres_log",
            workload_outputs=result["outputs"],
            skipped_statements=result["skipped"][:100],
            skipped_summary=result["skipped_summary"],
        )

    def _parse_transactions(
        self,
        target: Path,
        *,
        max_lines: int,
    ) -> tuple[dict[str, list[str]], list[TransactionObservation]]:
        lines_processed = 0
        open_transactions: dict[str, list[str]] = {}
        finished_transactions: list[TransactionObservation] = []
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                if lines_processed >= max_lines:
                    break
                lines_processed += 1
                match = STATEMENT_RE.search(raw_line)
                if not match:
                    continue
                sql = match.group("sql").strip()
                pid = self._pid(raw_line)
                keyword = sql.rstrip(";").strip().upper()
                if keyword == "BEGIN":
                    open_transactions[pid] = []
                    continue
                if keyword in {"COMMIT", "ROLLBACK"}:
                    statements = open_transactions.pop(pid, [])
                    finished_transactions.append(
                        TransactionObservation(pid=pid, statement_count=len(statements), statements=statements[:10])
                    )
                    continue
                if pid in open_transactions:
                    open_transactions[pid].append(sql)
        return open_transactions, finished_transactions

    def _query_observations(self, workloads: dict) -> list[QueryObservation]:
        all_entries = [
            entry
            for entries in workloads.values()
            for entry in entries
        ]
        total_count = sum(entry.count for entry in all_entries)
        observations: list[QueryObservation] = []
        for entry in sorted(all_entries, key=lambda item: (-item.count, item.database_name, item.fingerprint))[:10]:
            observations.append(
                QueryObservation(
                    fingerprint=entry.fingerprint,
                    sample_sql=entry.example_raw_sqls[0] if entry.example_raw_sqls else entry.normalized_sql,
                    count=entry.count,
                    total_duration_ms=round(entry.total_duration_ms, 3),
                    proportion=round(entry.count / total_count, 6) if total_count else 0.0,
                    database_name=entry.database_name,
                    normalized_sql=entry.normalized_sql,
                    raw_sql=entry.example_raw_sqls[0] if entry.example_raw_sqls else "",
                    parameters=[asdict(parameter) for parameter in entry.parameters],
                )
            )
        return observations

    def _legacy_parse(self, path: str | Path, max_lines: int = 5000) -> LogSummary:
        target = Path(path).expanduser().resolve()
        lines_processed = 0
        statements_detected = 0
        query_counts: Counter[str] = Counter()
        query_samples: dict[str, str] = {}
        query_durations: defaultdict[str, float] = defaultdict(float)
        open_transactions: dict[str, list[str]] = {}
        finished_transactions: list[TransactionObservation] = []

        for raw_line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            if lines_processed >= max_lines:
                break
            lines_processed += 1
            match = STATEMENT_RE.search(raw_line)
            if not match:
                continue

            sql = match.group("sql").strip()
            duration = float(match.group("duration"))
            normalized = self._fingerprint(sql)
            pid = self._pid(raw_line)

            statements_detected += 1
            query_counts[normalized] += 1
            query_samples.setdefault(normalized, sql)
            query_durations[normalized] += duration

            keyword = sql.rstrip(";").strip().upper()
            if keyword == "BEGIN":
                open_transactions[pid] = []
                continue
            if keyword in {"COMMIT", "ROLLBACK"}:
                statements = open_transactions.pop(pid, [])
                finished_transactions.append(
                    TransactionObservation(pid=pid, statement_count=len(statements), statements=statements[:10])
                )
                continue
            if pid in open_transactions:
                open_transactions[pid].append(sql)

        total_count = sum(query_counts.values())
        top_queries = [
            QueryObservation(
                fingerprint=fingerprint,
                sample_sql=query_samples[fingerprint],
                count=count,
                total_duration_ms=round(query_durations[fingerprint], 3),
                proportion=round(count / total_count, 6) if total_count else 0.0,
            )
            for fingerprint, count in query_counts.most_common(10)
        ]
        sampled_transactions = finished_transactions[:10]
        multi_statement_transactions = sum(1 for tx in finished_transactions if tx.statement_count > 1)
        return LogSummary(
            path=str(target),
            lines_processed=lines_processed,
            statements_detected=statements_detected,
            transactions_detected=len(finished_transactions),
            multi_statement_transactions=multi_statement_transactions,
            top_queries=top_queries,
            sampled_transactions=sampled_transactions,
            source_kind="postgres_log",
        )

    def _fingerprint(self, sql: str) -> str:
        compact = WS_RE.sub(" ", sql.strip())
        compact = re.sub(r"\b\d+\b", "?", compact)
        compact = re.sub(r"'[^']*'", "'?'", compact)
        return compact

    def _pid(self, line: str) -> str:
        match = PID_RE.search(line)
        return match.group("pid") if match else "unknown"
