# IlvesBench evaluation with Gemma-4-26B-A4B-it-AIC

## Evaluation setup

The evaluation used the hosted `Gemma-4-26B-A4B-it-AIC` endpoint provided by TUNI Aviary through its OpenAI-compatible API. The same IlvesBench normalization, migration, and query-rewrite prompts were used for all databases. PostgreSQL executed the generated artifacts. Human decisions were limited to approving evidence-backed 1NF findings and rejecting optional summary-table recommendations so that the experiment measured normalization, migration, and query rewriting rather than workload-specific materialization.

The databases and workloads were:

- DVD Rental: the standard 15-table database and 10 queries.
- IMDb 5000: seven source tables and 10 queries.
- NYC Yellow 5000: only `source_5000_yellow_trips`, containing exactly 5,000 rows, and five queries.

## Main results

| Database | LLM | Normalization | Data migration | Query rewrites |
|---|---|---|---|---|
| DVD Rental | Gemma-4-26B-A4B-it-AIC | Raw model: fail; system-assisted: pass | Pass | 10/10 (100%) |
| IMDb 5000 | Gemma-4-26B-A4B-it-AIC | Raw model: fail; system-assisted: pass | Pass | 10/10 (100%) |
| NYC Yellow 5000 | Gemma-4-26B-A4B-it-AIC | Pass: evidence-based no-change decision | Pass as no-op/copy control | 5/5 (100%) |

“System-assisted” means that the LLM did not independently produce the final valid normalization artifact. IlvesBench supplied deterministic findings, PostgreSQL validated the generated schema and migration, and a human approved only the findings supported by those observations.

## Results by database

### DVD Rental

The normalization request took 13.42 seconds, but the hosted model exhausted its output budget while generating reasoning (`finish_reason = length`) and returned no answer content. Therefore, the raw model-only normalization result was classified as a failure. IlvesBench's deterministic 1NF scan detected the `film.special_features` array, and a human approved that single evidence-backed split. No candidate 3NF functional dependencies were accepted.

IlvesBench applied 16 schema statements and 16 migration statements. Independent validation matched all 15 source/target parent-table counts and all 2,115 expected `film_special_features` rows. Query rewriting took 6.25 seconds. All 10 rewritten statements executed successfully, and their column names and ordered rows exactly matched the source results.

### IMDb 5000

The normalization request took 12.78 seconds and again ended with `finish_reason = length`, reasoning content, and no answer content. The raw model-only normalization result was therefore a failure. The deterministic scan identified five multi-valued attributes: `name_basics.primaryprofession`, `name_basics.knownfortitles`, `title_basics.genres`, `title_crew.writers`, and `title_crew.directors`. A human approved the three table-level candidates containing these five findings.

IlvesBench applied 12 schema statements and 12 migration statements. Independent checks matched all seven parent-table counts and every expected child-table count: 11,082 professions, 22,547 known-title references, 9,492 genres, 3,739 writer references, and 4,869 director references. Query rewriting took 5.28 seconds. All 10 queries matched exactly in columns and ordered rows.

### NYC Yellow 5000

The deterministic scan found no common 1NF warning. The model returned a valid `insufficient_evidence` assessment in 6.35 seconds, correctly avoiding an unsupported decomposition because the sample table had no declared primary key, unique constraint, or demonstrated functional dependency. The empty target was populated with an exact copy of only the 5,000-row yellow-taxi sample; no normalization migration was needed.

Query rewriting took 4.31 seconds. Gemma returned all five SQL statements unchanged, which is appropriate for an unchanged target schema. IlvesBench reported 5/5 statements executable. The source and target tables matched as complete multisets. An initial strict comparison matched 3/5 ordered results, while an immediate repeat matched 5/5. The first mismatch affected queries 3 and 4, whose `ORDER BY` clauses do not fully order tied groups before `LIMIT`; PostgreSQL can therefore select different tied rows on separate executions even when the SQL and relational content are identical. This is a workload determinism issue, not evidence of an incorrect rewrite.

## Interpretation

The evaluation supports a component-level conclusion rather than a claim that the LLM works perfectly. Gemma generated valid query-rewrite output for all 25 workload queries and made a defensible no-change decision for NYC. However, it did not independently complete the two more demanding normalization responses within the configured output budget. DVD Rental and IMDb succeeded only because IlvesBench combined deterministic 1NF detection, human approval, deterministic DDL/data migration, and PostgreSQL validation.

Thus, the result demonstrates that the overall IlvesBench workflow can produce usable artifacts with this model, while also showing why raw LLM success and system-assisted success must be reported separately.

## Validity notes

- The reported query rate is 25/25 executable and empirically equivalent for the tested data. It is not a formal proof of equivalence for every possible database state.
- The NYC workload should add complete tie-breakers to queries 3 and 4 if exact ordered-result reproducibility is required.
- The NYC migration is a no-change control: the table was copied exactly because no evidence-supported decomposition was proposed.
- Results describe this model endpoint, prompt configuration, database contents, and run date. Hosted model behavior can change across deployments.
- `Gemma-4-26B-A4B-it-AIC` is evaluated here as a hosted large open-weight model; it does not replace the separate proprietary-model condition requested for the journal study.

## Run identifiers

| Database | Run ID | Target database |
|---|---|---|
| DVD Rental | `run-42d081fd73ad` | `dvdrental_ilvesbench_gemma_4_26b_a4b_it_aic` |
| IMDb 5000 | `run-1f69b667bf4c` | `imdb_5000_ilvesbench_gemma_4_26b_a4b_it_aic` |
| NYC Yellow 5000 | `run-0fa74388e865` | `nyc_yellow_5000_ilvesbench_gemma_4_26b_a4b_it_aic` |
