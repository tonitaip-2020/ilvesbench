# IlvesBench 0.1

IlvesBench is a system for automated PostgreSQL benchmarking and schema-evolution analysis.

The current implementation follows the following architecture:

- `llm/`: interchangeable planner gateway clients
- `agent/`: orchestration, run-state tracking, and policy enforcement
- `db/`: PostgreSQL inspection and database-side tool wrappers
- `osops/`: query-log parsing, hardware inspection, and subprocess wrappers
- `benchmark/`: deterministic benchmark runners and future workload/schema modules
- `store/`: durable run metadata and JSON artifact persistence
- `api/`: lightweight HTTP API for a browser UI

The LLM is intentionally constrained to planner/interpreter work. It never talks to PostgreSQL or the OS directly. All stateful actions go through typed Python tools.

## Current MVP

The current version is narrow and deterministic to a degree. It can:

- load a TOML config
- talk to an Aviary/OpenAI-compatible local LLM endpoint
- inspect PostgreSQL schema metadata
- read and summarize one PostgreSQL log file
- accept a human-provided workload SQL file when logs are unavailable
- capture one hardware snapshot
- run workload-driven `pgbench` benchmarks against `db-original` and `db-new`
- persist benchmark run records and artifacts
- expose the results in a minimal browser UI

The higher-risk steps are still explicit and reviewable. Schema creation, data migration, and pgbench execution remain approval-gated actions, while index, tuning, metrics, energy, and comparison steps produce artifacts automatically for review.

The current normalization step is currently a conservative metadata-only first pass. It can:

- flag obvious repeating-group patterns such as numbered columns
- flag duplicated descriptor columns next to foreign-key identifiers
- report when the schema appears keyed and has no obvious warning signs
- report when metadata is insufficient for a confident 3NF assessment

Recent feature additions make the MVP path more complete. IlvesBench now also emits:

- workload-aware index recommendations with `CREATE INDEX IF NOT EXISTS` statements
- conservative `postgresql.conf` tuning recommendations and equivalent `ALTER SYSTEM` statements
- extended PostgreSQL metrics such as table size, heap/index size, table scan counters, database activity, and cache-hit ratio
- energy estimates for completed `pgbench` runs based on configured or CPU-derived wattage
- before/after comparison artifacts for db-original versus db-new normalized rewritten workload benchmarks

## Project layout

- `src/ilvesbench/`: application package
- `tests/`: unit tests for the orchestrator and log parser
- `ilvesbench.example.toml`: sample config for local development
- `docker-compose.yml`: PostgreSQL starter stack
- `postgres-config/`: starter PostgreSQL config files
- `init-db/`: initialization SQL hooks

## Secrets and local config

Do not commit LLM API keys. The sample config points to an ignored local secret file:

```bash
mkdir -p secrets
printf 'sk-your-key-here' > secrets/aviary_api_key
```

IlvesBench resolves the LLM key in this order:

- `ILVESBENCH_LLM_API_KEY` environment variable
- `[llm].api_key_file` in the TOML config
- `[llm].api_key` in the TOML config

For GitHub, keep committed files as examples only. Put personal overrides in `ilvesbench.toml`, `*.local.toml`, `.env`, or `secrets/`; these paths are ignored by Git.

## Run the web app

1. Create a virtual environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

And clone this repository.

2. If you want live PostgreSQL inspection, also install the PostgreSQL client dependency:

```bash
pip install "psycopg[binary]>=3.2,<4"
```

3. Start PostgreSQL with Docker (if needed):

```bash
docker compose up -d
```

4. Launch the IlvesBench web server:

```bash
python3 run_ilvesbench.py serve --config ilvesbench.example.toml
```

5. Open the printed local URL, usually:

```text
http://127.0.0.1:8080
```

## CLI examples

Run a single MVP collection pass:

```bash
python3 run_ilvesbench.py run --config ilvesbench.example.toml
```

Test only the LLM gateway:

```bash
python3 run_ilvesbench.py test-llm --config ilvesbench.example.toml
```

## Notes on Python environments

- The direct launcher `run_ilvesbench.py` works from the repository root without installing the package.
- `python3 -m ilvesbench ...` requires a working package install in the active virtual environment.
- On some Python 3.13 setups, especially offline ones, `pip install -e .` can fail or create an editable install that does not resolve the `src/` path correctly.
- If that happens, use `python3 run_ilvesbench.py ...` instead.

## Workload input flow

- IlvesBench first looks for the configured PostgreSQL log file.
- If the log file is missing, you can provide a workload SQL file from the web UI.
- If you leave the web field blank, IlvesBench uses `data/workload.sql` by default.
- The workload file should contain SQL statements separated by semicolons.
- If neither source exists, the run ends in `awaiting_input` and the UI explains what to provide next.

## Enable pgbench

`pgbench` is controlled by the `[pgbench]` section in your TOML config. A working setup looks like this:

```toml
[pgbench]
enabled = true
command = "pgbench"
duration_seconds = 30
clients = 4
jobs = 1
```

Notes:

- IlvesBench runs `pgbench` for 30 seconds against `db-original` when you press the benchmark button. This is too short timeframe for real benchmarking. This should be configurable by the user.
- After approved schema creation, data migration, and workload rewriting, it can run `pgbench` against `db-new` from the UI.
- The `db-original` run uses the workload SQL file passed in the UI, or `data/workload.sql` if the field is left blank.
- The `db-new` run uses an LLM-rewritten workload artifact generated from the original workload and normalized target schema.
- `pgbench` must be installed on the host machine because IlvesBench runs on the host.

## Benchmark comparison flow

1. Start an MVP run from the CLI or UI.
2. Review the generated normalization, migration, rewritten workload, index, tuning, and metrics artifacts.
3. In the UI, run the approval actions in order: create db-new schema, migrate data, run pgbench on db-original, then run pgbench on db-new.
4. IlvesBench writes a `benchmark_comparison.json` artifact comparing throughput, latency, storage size, and energy-per-transaction when both pgbench runs complete.

Energy estimates are controlled by the `[energy]` section:

```toml
[energy]
enabled = true
estimated_cpu_watts = 45.0
estimated_watts_per_cpu = 12.0
co2_grams_per_kwh = 110.0
```

## Run tests

```bash
python3 -m unittest discover -s tests
```

## Design notes

- Reproducibility: each run records config path, step states, artifacts, model settings, and tool outputs.
- Safety: destructive steps are present as explicit approval-gated pipeline stages instead of hidden side effects.
- Determinism: the executed MVP path is a normal Python workflow that can run without the LLM.
- Extensibility: the schema transformer, migrator, and richer metrics collectors already have module boundaries, so later work can fill them in without reshaping the whole codebase.

## TODOs

- **Normalization & DB structure transformation**:
- **Query rewriting**:
  - Validation of queries, DB structure and data migration. Generate simple tests.
- **Data migrations**:
  - Collect metadata on how much the database takes disk space, indices included.
- **New index creation**:
  - Based on rewritten queries, suggest secondary indices. "Click to create".
- **New table recommendations (outside normalization)**:
  - Based on rewritten queries and query logs, suggest summary tables.
- **OS/Hardware**:
  - Needs robust reading from Linux. Does not read from Windows.
- **OS/query logs**:
  - Design log analysis. Logs can be very large, not feasible to send to LLM. Needs to be deterministic.
- **OS/postgresql.conf**:
  - Provide recommendations for the file based on hardware. "Click to apply changes".
- **Architecture**:
  - In the future, consider to change the architecture to use a more complex framework like LangChain to avoid growth problems. Right now, this risks accidentally drifting toward "LLM agent improvises actions", which is exactly what we want to avoid. Right now, LangChain will introduce more abstraction where we want to avoid abstractions, and makes debugging harder when SQL/schema generation fails.
- **Benchmarking**:
  - Generating `pgbench` workloads with the LLM component.
  - Making `pgbench` easily replaceable.
  - Making `pgbench` configurable, and defaul configuration based on hardware.
- **GUI redesign**:
  - Visualizing results
  - GUI is too heavy, clogs browser easily
  - GUI re-design, perhaps a step-by-step, "tabs" approach?

#   i l v e s b e n c h - v 2 
 
 #   i l v e s b e n c h 
 
 
