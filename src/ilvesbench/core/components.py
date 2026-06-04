from __future__ import annotations

from dataclasses import dataclass

from ilvesbench.benchmarker.service import BenchmarkerService
from ilvesbench.dbops.service import DBOpsService
from ilvesbench.orchestrator.llm_tasks import LLMTaskService
from ilvesbench.osops.service import OSOpsService


@dataclass(frozen=True)
class ComponentBundle:
    """Runtime component bundle used by the orchestrator.

    Keeping the bundle explicit makes it easier to replace individual components
    in tests or future deployments without changing the workflow coordinator.
    """

    dbops: DBOpsService
    osops: OSOpsService
    benchmarker: BenchmarkerService
    llm_tasks: LLMTaskService

