from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re

from ilvesbench.models import LogSummary, QueryObservation


LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
WS_RE = re.compile(r"\s+")


class WorkloadFileParser:
    def parse(self, path: str | Path) -> LogSummary:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"Workload file not found: {target}")

        raw = target.read_text(encoding="utf-8", errors="replace")
        stripped = LINE_COMMENT_RE.sub("", raw)
        stripped = BLOCK_COMMENT_RE.sub("", stripped)

        query_counts: Counter[str] = Counter()
        query_samples: dict[str, str] = {}
        for statement in self._split_statements(stripped):
            normalized = self._fingerprint(statement)
            query_counts[normalized] += 1
            query_samples.setdefault(normalized, statement)

        top_queries = [
            QueryObservation(
                fingerprint=fingerprint,
                sample_sql=query_samples[fingerprint],
                count=count,
                total_duration_ms=0.0,
            )
            for fingerprint, count in query_counts.most_common(10)
        ]

        return LogSummary(
            path=str(target),
            lines_processed=len(raw.splitlines()),
            statements_detected=sum(query_counts.values()),
            transactions_detected=0,
            multi_statement_transactions=0,
            top_queries=top_queries,
            sampled_transactions=[],
            source_kind="workload_file",
        )

    def _split_statements(self, text: str) -> list[str]:
        statements: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False

        for char in text:
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double

            if char == ";" and not in_single and not in_double:
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                continue

            current.append(char)

        tail = "".join(current).strip()
        if tail:
            statements.append(tail)
        return statements

    def _fingerprint(self, sql: str) -> str:
        compact = WS_RE.sub(" ", sql.strip())
        compact = re.sub(r"\b\d+\b", "?", compact)
        compact = re.sub(r"'[^']*'", "'?'", compact)
        return compact
