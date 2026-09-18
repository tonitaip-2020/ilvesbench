# IlvesBench LLM evaluation datasets

This directory contains database-specific IlvesBench configuration and workload files for the constructive-study evaluation.

## Included databases

- `dvdrental`: compact relational control with 15 tables and an explicit 1NF candidate in `film.special_features` (`text[]`).
- `nyc_tlc_2026`: large flat analytical dataset containing yellow, green, FHV, and high-volume FHV trips. The largest table is approximately 84 million rows.

Both configurations disable `pgbench` and energy estimation for the initial LLM-component evaluation. Enable performance testing only after normalization, migration, query validation, and result-equivalence checks are complete.

## Required PostgreSQL permissions

The imported databases and tables are owned by `postgres`, while IlvesBench currently connects as `llm`. Before running an evaluation, use pgAdmin as `postgres` to grant source read access:

```sql
GRANT CONNECT ON DATABASE dvdrental TO llm;
GRANT CONNECT ON DATABASE nyc_tlc_2026 TO llm;
```

Then connect pgAdmin's Query Tool to **each source database separately** and run:

```sql
GRANT USAGE ON SCHEMA public TO llm;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO llm;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO llm;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT ON TABLES TO llm;
```

Create the target databases as `postgres`:

```sql
CREATE DATABASE dvdrental_ilvesbench OWNER llm;
CREATE DATABASE nyc_tlc_2026_ilvesbench OWNER llm;
```

Connect to each target database separately and prepare the foreign-data wrapper used by IlvesBench:

```sql
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO llm;
GRANT ALL ON SCHEMA public TO llm;
```

## OpenAI API key

Save the API key as a single line in:

```text
secrets/openai_api_key
```

The `secrets/` directory is ignored by Git. Do not put the key directly in a committed TOML file.

## Initial runs

From the repository root:

```powershell
.venv\Scripts\python.exe run_ilvesbench.py evaluation\dvdrental\ilvesbench.toml
```

For NYC TLC:

```powershell
.venv\Scripts\python.exe run_ilvesbench.py evaluation\nyc_tlc_2026\ilvesbench.toml
```

The NYC configuration targets the full dataset. For the first migration test, create and document a reproducible subset rather than copying all rows. Use the full database later for the scalability experiment.

## Reporting

Record at minimum: model identifier, run date, database, task, initial success, success after PostgreSQL-guided repair, repair count, final PostgreSQL executability, result-equivalence status, input/output tokens, and latency. Query-rewrite success is the number of semantically equivalent target queries divided by the total number of source queries.
