# IlvesBench

<p align="center">
  <img src="docs/assets/ilvesbench-logo.png" alt="IlvesBench logo" width="250">
</p>

IlvesBench is a human-guided PostgreSQL benchmarking and schema-evolution tool. It helps researchers and database practitioners evaluate how normalization, query rewrites, indexing, summary tables, and configuration choices affect a database workload.

It profiles an existing source database, derives or ingests a representative workload, helps construct and validate a target database, and compares the two with `pgbench`. The web interface keeps generated SQL visible, editable, and approval-gated before database changes are applied. Results include throughput, latency, storage metrics, and—when optional NETIO hardware is configured—measured power and energy data, including post-run TPS and power-over-time charts.

This matters because schema and physical-design changes are often evaluated using incomplete workloads or isolated metrics. IlvesBench provides a repeatable workflow that connects database structure, real query behavior, and performance and energy measurements.

## Scope and limitations

- PostgreSQL only.
- IlvesBench assumes that a source database already exists, or is otherwise available in a form that can be loaded into PostgreSQL.
- It is a decision-support and benchmarking tool, not an autonomous database administrator: schema changes, data migration, index changes, and other consequential actions require user review and approval.
- Benchmark results depend on the workload, hardware, PostgreSQL configuration, and benchmark settings. They should be interpreted as comparative evidence for the tested environment.

## Install and run

Prerequisites: Python 3.11+, PostgreSQL client tools including `pgbench`, and optionally Docker for the bundled PostgreSQL stack.

```bash
git clone https://github.com/tonitaip-2020/ilvesbench.git
cd ilvesbench
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[postgres]"
```

Create a local configuration file from the example and add your PostgreSQL connection details and LLM credentials:

```bash
cp ilvesbench.example.toml ilvesbench.toml
python3 run_ilvesbench.py serve --config ilvesbench.toml
```

Open the address configured under `[web]` (the example uses `http://127.0.0.1:8081`). If you need the bundled database stack, start it first with:

```bash
docker compose up -d
```

Use the browser interface to select the source and target databases, inspect the source, prepare or import a workload, review generated SQL, and run the source and target benchmarks.

### Optional NETIO energy measurement

To record power measurements during `pgbench`, configure a NETIO 4KF or compatible PowerBOX in `ilvesbench.toml`:

```toml
[netio]
url = "http://192.168.1.78/netio.json"
username = "netio"
password = "netio"
poll_interval_seconds = 1.0
timeout_seconds = 5.0
```

NETIO is optional. Without it, IlvesBench continues to benchmark normally and reports that no energy-measurement hardware is configured.

## Disclaimer

This software is provided "as is", without warranty of any kind, express or implied. By using, modifying, or distributing this software, you acknowledge and agree that you do so entirely at your own risk. The authors, contributors, and maintainers shall not be liable for any claim, damages, loss, or other liability arising from or related to the use of this software. Do not run IlvesBench in a production environment.
