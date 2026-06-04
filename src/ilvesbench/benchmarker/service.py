from __future__ import annotations

from ilvesbench.benchmark.energy import BenchmarkComparator, EnergyEstimator
from ilvesbench.benchmark.index_advisor import IndexAdvisor
from ilvesbench.benchmark.pgbench import PgBenchRunner
from ilvesbench.benchmark.tuning import PostgresTuningAdvisor
from ilvesbench.benchmark.workload import WorkloadPlanner
from ilvesbench.config import IlvesBenchConfig
from ilvesbench.llm.gateway import LLMGateway


class BenchmarkerService:
    """Benchmarking boundary for workload generation and pgbench execution."""

    def __init__(
        self,
        config: IlvesBenchConfig,
        *,
        llm: LLMGateway | None = None,
        pgbench: PgBenchRunner | None = None,
    ) -> None:
        self.pgbench = pgbench or PgBenchRunner()
        self.workload_planner = WorkloadPlanner(
            llm=llm,
            target_database=config.postgres.new_database,
        )
        self.index_advisor = IndexAdvisor()
        self.tuning_advisor = PostgresTuningAdvisor()
        self.energy_estimator = EnergyEstimator()
        self.benchmark_comparator = BenchmarkComparator()

