import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

import psycopg


CONNECTION = {"host": "127.0.0.1", "port": 5432, "user": "llm", "password": "llm"}
SOURCE_DATABASE = "nyc_yellow_march_2026_5000"
TABLE = "public.yellow_taxi_trips"


def canonical(rows):
    return Counter(tuple(None if value is None else str(value) for value in row) for row in rows)


def load_results(arguments):
    if arguments.artifact:
        payload = json.loads(Path(arguments.artifact).read_text(encoding="utf-8"))
        return payload["ordered_results"]
    with sqlite3.connect(arguments.state) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (arguments.run_id,)
            ).fetchone()[0]
        )
    step = next(item for item in payload["steps"] if item["name"] == "rewrite_queries")
    return step["details"]["query_results"]


parser = argparse.ArgumentParser()
parser.add_argument("--label", required=True)
parser.add_argument("--target", required=True)
parser.add_argument("--artifact")
parser.add_argument("--state")
parser.add_argument("--run-id")
parser.add_argument("--output", required=True)
args = parser.parse_args()
results = sorted(load_results(args), key=lambda item: int(item["query_index"]))

validation = []
for item in results:
    index = int(item["query_index"])
    source_sql = item["source_statement"]
    target_sql = item["rewritten_statement"]
    kind = source_sql.lstrip().split(None, 1)[0].upper()
    outcome = {
        "query_index": index,
        "kind": kind,
        "sql_identical": source_sql.strip() == target_sql.strip(),
        "passed": False,
    }
    source = psycopg.connect(dbname=SOURCE_DATABASE, **CONNECTION)
    target = psycopg.connect(dbname=args.target, **CONNECTION)
    try:
        if kind == "SELECT":
            with source.cursor() as cursor:
                cursor.execute(source_sql)
                source_columns = [column.name for column in cursor.description]
                source_rows = cursor.fetchall()
            with target.cursor() as cursor:
                cursor.execute(target_sql)
                target_columns = [column.name for column in cursor.description]
                target_rows = cursor.fetchall()
            outcome.update(
                columns_equal=source_columns == target_columns,
                exact_order_equal=source_rows == target_rows,
                unordered_equal=canonical(source_rows) == canonical(target_rows),
                source_row_count=len(source_rows),
                target_row_count=len(target_rows),
            )
            outcome["passed"] = outcome["columns_equal"] and outcome["unordered_equal"]
        else:
            with source.cursor() as cursor:
                cursor.execute(source_sql)
                source_affected = cursor.rowcount
            with target.cursor() as cursor:
                cursor.execute(target_sql)
                target_affected = cursor.rowcount
            source_rows = source.execute(f"SELECT * FROM {TABLE}").fetchall()
            target_rows = target.execute(f"SELECT * FROM {TABLE}").fetchall()
            outcome.update(
                source_affected=source_affected,
                target_affected=target_affected,
                affected_equal=source_affected == target_affected,
                post_state_equal=canonical(source_rows) == canonical(target_rows),
                source_post_count=len(source_rows),
                target_post_count=len(target_rows),
            )
            outcome["passed"] = outcome["affected_equal"] and outcome["post_state_equal"]
    except Exception as exc:
        outcome["error"] = str(exc)
    finally:
        source.rollback()
        target.rollback()
        source.close()
        target.close()
    validation.append(outcome)
    print(
        args.label,
        f"Q{index}",
        kind,
        "PASS" if outcome["passed"] else "FAIL",
        json.dumps({key: value for key, value in outcome.items() if key not in {"query_index", "kind", "passed"}}),
    )

selects = [item for item in validation if item["kind"] == "SELECT"]
dml = [item for item in validation if item["kind"] != "SELECT"]
summary = {
    "label": args.label,
    "target_database": args.target,
    "select_passed": sum(item["passed"] for item in selects),
    "select_total": len(selects),
    "dml_passed": sum(item["passed"] for item in dml),
    "dml_total": len(dml),
    "overall_passed": sum(item["passed"] for item in validation),
    "overall_total": len(validation),
    "results": validation,
}
Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")
print("SUMMARY", json.dumps({key: value for key, value in summary.items() if key != "results"}))
