# IlvesBench evaluation: DVD Rental with Ministral 3 8B

## Evaluation condition

- Source database: `dvdrental`
- Current target database: `dvdrental_ilvesbench_ministral_3_8b`
- Target name stored in the original run artifacts: `dvdrental_ilvesbench_llama32_3b` (renamed after evaluation)
- Model: `ministral-3:8b`, served locally through Ollama
- IlvesBench run ID: `run-33f2d9df9cab`
- Workload: 10 queries from `evaluation/dvdrental/workload.sql`
- Prompt condition: corrected normalization, migration, and rewrite prompts
- Optional summary tables: all eight candidates rejected so that rewrites were evaluated against the normalized base tables

## Result for the supervisor's table

| Database | LLM | Normalization | Data migration | Query rewrites |
|---|---|---|---|---:|
| DVD Rental | Ministral 3 8B Instruct | Fail model-only; pass system-assisted | Pass after prerequisite repair | 100% (10/10) |

This row deliberately separates the raw LLM result from the completed IlvesBench workflow. Writing only “pass” for normalization would conceal that Ministral ignored the required JSON format and that deterministic analysis plus human review supplied the executable design.

## Normalization result

IlvesBench detected one evidence-backed 1NF issue: `public.film.special_features` is a `text[]` collection, and 187 of 250 sampled non-null rows (74.8%) contained multiple values. Ministral correctly described the semantic repair—moving the array values into a child relation—but returned prose and Markdown SQL instead of the required JSON object. Therefore:

- Raw LLM normalization artifact: **fail** (structured-output failure).
- Semantic idea in the response: **appropriate**, because it identified the same array decomposition.
- Completed pipeline normalization: **pass, system-assisted**. IlvesBench generated the conservative deterministic proposal, a human approved the finding, and the resulting design retained `film` without `special_features` and added `film_special_features(film_id, special_feature)`.
- Subsequent functional-dependency discovery found no evidence-backed additional 3NF decompositions.

The normalization request took approximately 3 minutes 40 seconds. The follow-up functional-dependency request took approximately 6 minutes 13 seconds on the local model.

## Data-migration result

IlvesBench generated and executed 16 deterministic migration statements. The first schema-creation attempt failed because copied DVD Rental defaults referenced source-defined PostgreSQL objects that did not yet exist in the empty target database. The repair created 13 sequences, the `year` domain, and the `mpaa_rating` enum before replaying the same generated schema.

After this prerequisite repair:

- Target-schema creation: **pass** (16 schema statements).
- Migration execution: **pass** (16 migration statements).
- Copied source-table counts: **15/15 matched**.
- Extracted `film_special_features` rows: **2,115 expected and 2,115 migrated**.

This is classified as a system-assisted migration pass, not an unaided model pass, because PostgreSQL prerequisite objects were supplied outside the generated DDL.

## Query-rewrite result

IlvesBench generated rewrites for all 10 workload queries and validated them on PostgreSQL. A separate strict comparison then executed every original query on `dvdrental` and every rewrite on the normalized target. A query passed only when both the ordered output-column names and the complete ordered row results were identical.

| Query | PostgreSQL executable | Columns/order equal | Rows equal | Result rows | Strict result |
|---:|---|---|---|---:|---|
| 1 | Yes | Yes | Yes | 10 | Pass |
| 2 | Yes | Yes | Yes | 33 | Pass |
| 3 | Yes | Yes | Yes | 16 | Pass |
| 4 | Yes | Yes | Yes | 2 | Pass |
| 5 | Yes | Yes | Yes | 100 | Pass |
| 6 | Yes | Yes | Yes | 100 | Pass |
| 7 | Yes | Yes | Yes | 100 | Pass |
| 8 | Yes | Yes | Yes | 100 | Pass |
| 9 | Yes | Yes | Yes | 190 | Pass |
| 10 | Yes | Yes | Yes | 5 | Pass |

The important transformed case was query 6. Ministral replaced the original array predicate with a join to `film_special_features`, while preserving the projected columns, ordering, limit, and result rows. The remaining queries were preserved or rewritten without changing their results.

The two rewrite batches took approximately 4 minutes 57 seconds and 2 minutes 36 seconds, respectively (about 7 minutes 33 seconds total).

## Interpretation

Ministral achieved **100% query-rewrite correctness for this 10-query DVD Rental workload**, but this does not mean that the entire workflow worked perfectly without assistance. Its query rewriting was successful, while its initial normalization response violated the structured-output contract and target-schema creation required deterministic PostgreSQL repair. The result supports the intended IlvesBench architecture: the LLM proposes semantic transformations, deterministic components and PostgreSQL enforce technical constraints, execution provides empirical feedback, and a human accepts or rejects semantic assumptions.

## Reproducibility artifacts

- Run state: `dvdrental_ministral_corrected_eval/run.sqlite3` (preserves the target's name at evaluation time)
- Raw requests and responses: `dvdrental_ministral_corrected_eval/artifacts/run-33f2d9df9cab/logs/llm/`
- Strict validation script: `validate_dvdrental_ministral_results.py`
- Target prerequisite repair: `prepare_dvdrental_target_prerequisites.py`
