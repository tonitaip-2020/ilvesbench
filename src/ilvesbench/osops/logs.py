from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re

from ilvesbench.models import LogSummary, QueryObservation, TransactionObservation


STATEMENT_RE = re.compile(
    r"duration:\s+(?P<duration>[0-9.]+)\s+ms\s+(?:execute\s+[^:]+:\s+|statement:\s+)(?P<sql>.+)$"
)
PID_RE = re.compile(r"\[(?P<pid>\d+)\]")
WS_RE = re.compile(r"\s+")


class PostgresLogParser:
    def parse(self, path: str | Path, max_lines: int = 5000) -> LogSummary:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"PostgreSQL log file not found: {target}")

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

        top_queries = [
            QueryObservation(
                fingerprint=fingerprint,
                sample_sql=query_samples[fingerprint],
                count=count,
                total_duration_ms=round(query_durations[fingerprint], 3),
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
