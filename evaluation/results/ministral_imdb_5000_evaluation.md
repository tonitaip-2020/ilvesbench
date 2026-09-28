# IlvesBench evaluation: IMDb 5000 with Ministral 3 8B

## Evaluation configuration

- Source database: `imdb_5000`
- Target database: `imdb_5000_ilvesbench_ministral_3_8b`
- Model: `ministral-3:8b`, served locally through Ollama
- IlvesBench run ID: `run-c42b23f1bcd9`
- Prompt condition: corrected normalization and query-rewrite prompts
- Included source schema: `public`
- Excluded schemas: `firstnf`, `secondnf`, and `fourthnf`, because these are pre-existing normalized reference designs rather than original input tables
- Included source tables: 7
- Included source rows: 73,429
- Workload: 10 manually defined `SELECT` queries
- Optional summary tables: all six candidates rejected

The workload contains five ordinary relational queries and five queries that directly exercise multivalued IMDb fields: genres, directors, writers, professions, and known-for titles. Every source query was executed successfully before the evaluation.

## Result for the evaluation table

| Database | LLM | Normalization | Data migration | Query rewrites |
|---|---|---|---|---:|
| IMDb 5000 | Ministral 3 8B Instruct | Fail model-only; pass system-assisted | Pass after privilege setup | 100% (10/10) |

The normalization cell separates the raw model response from the completed IlvesBench workflow. Describing normalization simply as an unaided pass would conceal that the model did not follow the structured-output contract.

## Normalization

IlvesBench detected five evidence-backed 1NF problems across three tables:

| Source table | Multivalued source column | Generated child table |
|---|---|---|
| `name_basics` | `primaryprofession` | `name_basics_primaryprofession` |
| `name_basics` | `knownfortitles` | `name_basics_knownfortitles` |
| `title_basics` | `genres` | `title_basics_genres` |
| `title_crew` | `writers` | `title_crew_writers` |
| `title_crew` | `directors` | `title_crew_directors` |

Ministral identified the same semantic issues and described suitable child tables, but returned prose and Markdown SQL instead of the required JSON object. It also stated that source identifiers were primary keys even though the loaded source tables had no declared primary-key constraints. Consequently:

- Raw LLM normalization result: **fail** because the response violated the output contract and included an unsupported constraint assumption.
- Semantic diagnosis: **substantially appropriate**, because all five multivalue fields were identified correctly.
- Completed IlvesBench normalization: **pass, system-assisted**. Deterministic evidence generated conservative decompositions and a human approved all three table-level candidates.
- Additional 3NF decomposition: none accepted. The corrected workflow did not adopt unsupported functional dependencies.

The normalization generation took approximately 5 minutes 25 seconds. Functional-dependency discovery took approximately 4 minutes 58 seconds.

## Schema creation and migration

IlvesBench created 12 target tables: seven copied parent tables and five child tables. It then generated 12 deterministic migration statements. The initial migration attempt was blocked because the `llm` role lacked database-level `CREATE` permission for the temporary FDW staging schema. After granting that operational prerequisite, the same migration completed.

### Parent-table row-count validation

| Table | Source rows | Target rows | Result |
|---|---:|---:|---|
| `name_basics` | 6,902 | 6,902 | Pass |
| `title_akas` | 18,718 | 18,718 | Pass |
| `title_basics` | 5,000 | 5,000 | Pass |
| `title_crew` | 5,000 | 5,000 | Pass |
| `title_episode` | 0 | 0 | Pass |
| `title_principals` | 35,750 | 35,750 | Pass |
| `title_ratings` | 2,059 | 2,059 | Pass |

### Child-table validation

| Child table | Expected distinct source values | Migrated rows | Result |
|---|---:|---:|---|
| `name_basics_primaryprofession` | 11,082 | 11,082 | Pass |
| `name_basics_knownfortitles` | 22,547 | 22,547 | Pass |
| `title_basics_genres` | 9,492 | 9,492 | Pass |
| `title_crew_writers` | 3,739 | 3,739 | Pass |
| `title_crew_directors` | 4,869 | 4,869 | Pass |

All seven parent counts and all five child counts matched. Migration is therefore a **system-assisted pass after environment privilege setup**; no generated migration SQL repair was required.

## Query-rewrite evaluation

Ministral returned ten executable statements in one batch. Queries 1–3 and 9–10 remained unchanged because the columns they use remained in the parent tables. Queries 4–8 were rewritten to use the five child relations.

Each original query was executed on `imdb_5000`, and each rewrite was executed on `imdb_5000_ilvesbench_ministral_3_8b`. A strict empirical pass required identical output-column names, identical column order, and identical ordered result rows.

| Query | Rewrite condition | PostgreSQL executable | Columns/order equal | Rows equal | Result rows | Strict result |
|---:|---|---|---|---|---:|---|
| 1 | Unchanged | Yes | Yes | Yes | 100 | Pass |
| 2 | Unchanged | Yes | Yes | Yes | 100 | Pass |
| 3 | Unchanged | Yes | Yes | Yes | 100 | Pass |
| 4 | Rewritten: genres | Yes | Yes | Yes | 100 | Pass |
| 5 | Rewritten: directors | Yes | Yes | Yes | 100 | Pass |
| 6 | Rewritten: professions | Yes | Yes | Yes | 100 | Pass |
| 7 | Rewritten: known-for titles | Yes | Yes | Yes | 100 | Pass |
| 8 | Rewritten: writers | Yes | Yes | Yes | 100 | Pass |
| 9 | Unchanged | Yes | Yes | Yes | 2 | Pass |
| 10 | Unchanged | Yes | Yes | Yes | 100 | Pass |

The result is **10/10 (100%) empirically equivalent rewrites on this database and workload**. The rewrite batch took approximately 4 minutes 17 seconds.

## Interpretation and limitation

This run supports the proposed division of responsibility. Ministral supplied useful semantic hypotheses and successful query rewrites, deterministic analysis identified the concrete multivalue evidence and generated the migration, PostgreSQL rejected missing operational permissions and validated executable SQL, and human review decided whether the decompositions were acceptable.

The 100% rewrite score does not establish formal equivalence for all possible database states. It means that the ten supplied workload queries produced identical interfaces and results on the evaluated IMDb 5000 instance. In particular, query 7 contained an unnecessary condition involving `title_akas`; it did not change the evaluated result but illustrates why execution-based equality should eventually be supplemented with full-query equivalence analysis.

## Reproducibility artifacts

- Configuration: `imdb_5000_ministral_corrected.toml`
- Query set: `evaluation/imdb_5000/queries.sql`
- Run state: `imdb_5000_ministral_corrected_eval/run.sqlite3`
- Raw model requests and responses: `imdb_5000_ministral_corrected_eval/artifacts/run-c42b23f1bcd9/logs/llm/`
- Migration-count validator: `validate_imdb_migration_counts.py`
- Strict query validator: `validate_imdb_ministral_results.py`
