# NYC Yellow Taxi March 2026 evaluation

This directory records the IlvesBench mixed-workload evaluation performed on a reproducible, workload-aware sample of 5,000 March 2026 NYC yellow-taxi trips. The source also contained the official 265-row TLC taxi-zone lookup table.

## Evaluation scope

The query set in queries.sql contains ten statements: six SELECT queries, one INSERT, two UPDATE statements, and one DELETE. The evaluated models were Ministral 3 8B Instruct and Gemma 4 26B.

For each model, IlvesBench attempted schema normalization, data migration, and workload rewriting. Read-query validation compared column metadata, row counts, values, and ordered results between the source and target. Each data-changing statement was executed independently inside a transaction; affected-row counts and the complete post-operation trip-table state were compared before both transactions were rolled back.

## Results

| LLM | Model-only normalization | System-assisted outcome | Migration | Rewrite execution | SELECT equivalence | DML state equivalence |
|---|---|---|---|---:|---:|---:|
| Ministral 3 8B Instruct | Pass: no decomposition required | Pass | Pass as no-op/copy control | 10/10 after batch-size repair | 6/6 | 4/4 |
| Gemma 4 26B | Fail: no usable normalization JSON | Pass: metadata fallback retained schema | Pass as no-op/copy control | 10/10 | 6/6 | 4/4 |

A no-op/copy migration means that the normalization stage did not produce a justified schema decomposition. IlvesBench therefore preserved the original tables and copied their contents to the target. This validates the migration and rewrite machinery for an unchanged-schema control; it does not demonstrate successful schema transformation.

Ministral's original ten-statement rewrite request timed out. The controlled retry divided the workload into five batches of two statements, after which all ten completed. This is reported as a system-assisted batching repair, not as an unqualified first-attempt success. Gemma produced no usable structured normalization artifact, so the deterministic metadata fallback conservatively retained the source schema.

## Run identifiers

| LLM | IlvesBench run ID | Target database |
|---|---|---|
| Ministral 3 8B Instruct | run-34c8070a3f64 | nyc_yellow_march_2026_5000_ministral |
| Gemma 4 26B | run-52b7d9f5d3e9 | nyc_yellow_march_2026_5000_gemma |

## Reproduction notes

Create a source database named nyc_yellow_march_2026_5000 with the tables public.yellow_taxi_trips and public.taxi_zones. Populate it with the documented 5,000-row sample and 265 taxi zones, then create one empty target database per model. Run IlvesBench with queries.sql and the model configuration under test. Do not commit provider API keys; store them outside Git and reference them through the local configuration.

The final validation must leave the source and both targets at 5,000 trip rows because every DML transaction is rolled back. The observed equivalence applies to this database state and workload; it is empirical evidence rather than a proof for every possible database state.

## Data source

The full trip records, taxi-zone lookup table, and data dictionary are published by the NYC Taxi and Limousine Commission: https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page
