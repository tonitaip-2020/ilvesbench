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
- pgbench script/workload preparation
- pgbench parameter handling
- pgbench execution
- benchmark result parsing
- energy estimates
- benchmark comparison
- workload-aware index recommendations until those move fully into DBOps

### Orchestrator

Packages: `ilvesbench.agent` and `ilvesbench.orchestrator`

The orchestrator coordinates component outputs and LLM tasks:

- run lifecycle and step state
- artifact persistence
- human review checkpoints
- prompt payload assembly and LLM calls
- routing outputs from LLM tasks back to DBOps, OSOps, and Benchmarker

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

