# IlvesBench Architecture

IlvesBench is organized around a thin workflow orchestrator and loosely coupled
component services. Components own facts and actions. The orchestrator owns
sequencing, LLM mediation, run state, and artifact flow.

## Components

### DBOps

Package: `ilvesbench.dbops`

DBOps owns work that requires PostgreSQL access:

- database discovery and connection checks
- schema and metadata inspection
- profiling row counts, indexes, table sizes, and database metrics
- normal-form warning scans that require sampled database values
- source-data consistency checks for candidate functional dependencies
- target database creation, truncation, and dropping
- DDL/DML execution
- data migration support
- query validation and result checks
- index and summary-table creation

The current `DBOpsService` wraps the PostgreSQL gateway while behavior is moved
out of the legacy orchestrator incrementally.

### OSOps

Package: `ilvesbench.osops`

OSOps owns host and filesystem work:

- PostgreSQL log-file discovery and parsing
- explicit workload SQL file parsing
- hardware and container inspection
- subprocess execution
- future `postgresql.conf` reading and modification

### Benchmarker

Package: `ilvesbench.benchmarker`

Benchmarker owns workload and benchmark execution:

- workload profile and summary-table candidate planning
- source-to-target query rewrite progress, batching, repair, and cache state
- pgbench script/workload preparation
- pgbench parameter handling
- pgbench execution
- benchmark result parsing
- energy estimates
- benchmark comparison
- workload-aware index recommendations, including LLM-guided create/drop proposals

### Orchestrator

Packages: `ilvesbench.agent` and `ilvesbench.orchestrator`

The orchestrator coordinates component outputs and LLM tasks:

- run lifecycle and step state
- artifact persistence
- human review checkpoints
- prompt payload assembly and LLM calls
- routing outputs from LLM tasks back to DBOps, OSOps, and Benchmarker

Normalization is staged and human-gated. IlvesBench first surfaces 1NF
findings such as delimited multi-value columns. Tables approved for 1NF
normalization then move to 3NF review: the LLM proposes candidate functional
dependencies, DBOps checks candidates against current source data when the
columns map to one source table, and the user approves or rejects each FD. The
final 3NF target schema is synthesized deterministically from approved FDs.

Normalization prompts are token-budgeted. IlvesBench sends the complete schema
when it fits the configured context budget. Otherwise, it groups whole tables
into relationship-aware chunks and includes a compact global catalog of declared
relationships and unverified same-name/type associations in every chunk. Tables
are atomic: IlvesBench never divides one table's columns across LLM requests. If
one table plus the required prompt and relationship context does not fit, the
workflow reports `requires_human_schema_partitioning` and produces no automatic
target DDL from the incomplete analysis. Functional-dependency discovery uses
the same boundary and falls back to one complete post-1NF target table per
request when a combined request is too large.

Index recommendation is also staged. The Benchmarker builds an LLM prompt from
the workload queries, target database structure, and current PostgreSQL indexes.
Large workloads and index sets are split into batches, with prior
recommendations carried forward to reduce redundant proposals. The LLM may
recommend compound, expression/function, partial, INCLUDE, and PostgreSQL
access-method-specific indexes, plus safe index drops. The user must approve
the generated index-change SQL before DBOps executes it in PostgreSQL.

The legacy `PipelineOrchestrator` is being thinned. New LLM-mediated task
wrappers live under `ilvesbench.orchestrator`.

### API and Frontend

Packages: `ilvesbench.api` and `ilvesbench.static`

The frontend should eventually consume backend-provided state and action
capabilities instead of reconstructing workflow rules in JavaScript. The API
should be the boundary that translates component state into UI-safe DTOs.

## Direction

The migration strategy is incremental:

1. Introduce component service facades.
2. Move concrete behavior from `PipelineOrchestrator` into those services.
3. Add backend action-capability DTOs.
4. Simplify the frontend so it renders state and invokes actions, rather than
   enforcing workflow rules itself.
5. Split or replace the browser UI without changing backend component contracts.
