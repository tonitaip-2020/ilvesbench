# Corrected-Prompt Ministral Evaluation: NYC Yellow Taxi, 5,000 Rows

## Correction objective

The original Ministral run failed because the normalization response contained malformed and truncated JSON, comments, hypothetical columns and unsupported dependencies. Its query rewrite also altered case-sensitive PostgreSQL identifiers.

IlvesBench's prompts were corrected before this run. The new rules require the model to:

- infer functional dependencies only from declared keys, unique constraints or explicit deterministic sample evidence;
- return `insufficient_evidence` rather than infer uniqueness from names or world knowledge;
- avoid hypothetical columns, lookup values and unsupported constraints;
- produce complete strict JSON without comments or trailing commas;
- preserve projection count and positional order;
- preserve exact PostgreSQL identifier spelling, case and quoting;
- return an unchanged SQL statement when its source and target objects are unchanged;
- never invent `_target`, `_new` or `_normalized` object names.

Two regression tests were added for these requirements. The complete IlvesBench suite passed: 94 tests.

## Controlled configuration

- Model: `ministral-3:8b`
- Current database: `nyc_tlc_2026_ilvesbench_ministral_3_8b`
- Database name stored in the original run artifacts: `nyc_tlc_2026_ilvesbench_qwen` (renamed after evaluation)
- Sample: the same 5,000 January 2026 yellow-taxi rows used for Qwen and the original Ministral run
- Workload: the same five yellow-taxi `SELECT` queries
- Timeout: 600 seconds
- PostgreSQL repair allowance: one attempt per invalid query
- Corrected run ID: `run-06b0ebfc8744`

## Results

| Model condition | Normalization assessment | Migration | PostgreSQL-valid rewrites | Content equivalence | Strict interface equivalence |
|---|---:|---:|---:|---:|---:|
| Ministral, original prompts | Fail | N/A | 80% (4/5) | 80% (4/5) | 80% (4/5) |
| Ministral, corrected prompts | Pass—no decomposition needed | No-op | 100% (5/5) | 100% (5/5) | 100% (5/5) |

## Normalization assessment

The corrected response contained no functional dependencies and no target tables. It explained that:

- no primary-key or unique-constraint evidence supports a functional dependency;
- sampled columns are atomic;
- no deterministic evidence supports transitive dependencies;
- no safe decomposition can be justified.

IlvesBench accepted the response directly as an LLM result with status `appears_3nf` and summary `no_decomposition_needed`. No DDL or data migration was required.

The model used a Markdown JSON fence despite the instruction to output unfenced JSON. The JSON inside the fence was complete and valid, and IlvesBench's parser recovered it. Therefore, the semantic and structural normalization decision passed, but the response retained a minor formatting deviation.

Because the schema was unchanged, migration is classified as a successful **no-op**, not as evidence that Ministral generated or executed transformation SQL.

## Query-rewrite validation

| Query | PostgreSQL execution | Columns and order | Ordered values | Outcome |
|---:|---|---|---|---|
| 1 | Passed | Exact | Exact, 12 rows | Success |
| 2 | Passed | Exact | Exact, 4 rows | Success |
| 3 | Passed | Exact | Exact, 250 rows | Success |
| 4 | Passed | Exact | Exact, 100 rows | Success |
| 5 | Passed | Exact | Exact, 100 rows | Success |

All five corrected-prompt rewrites preserved the complete SQL result interface. The earlier Query 3 error involving unquoted `PULocationID` and `DOLocationID` did not recur.

## Interpretation

The prompt correction improved Ministral's observed outcome from an unsafe normalization proposal and 80% strict rewrite success to a safe no-decomposition decision and 100% strict rewrite success.

This must be reported as a separate experimental condition rather than replacing the original result. The improvement demonstrates prompt sensitivity, not an intrinsic change in model capability. A defensible statement is:

> With the original IlvesBench prompts, Ministral failed normalization validation and achieved four of five strictly equivalent rewrites. After adding evidence requirements and exact PostgreSQL identifier-preservation rules, Ministral produced a safe no-decomposition decision and five of five strictly equivalent rewrites on the same 5,000-row sample. The corrected response retained a minor formatting deviation because its valid JSON was enclosed in Markdown fences.

## Reproducibility artifacts

- Corrected configuration: `yellow_5000_ministral_corrected.toml`
- Shared query set: `evaluation/nyc_yellow_5000/queries.sql`
- Run database: `yellow_5000_ministral_corrected_eval/run.sqlite3`
- Run artifacts: `yellow_5000_ministral_corrected_eval/artifacts/run-06b0ebfc8744/`
- Rewrite result: `yellow_5000_ministral_corrected_eval/artifacts/run-06b0ebfc8744/yellow_5000_rewrite_result.json`
- Exact-comparison script: `compare_yellow_5000_ministral_corrected_results.py`
