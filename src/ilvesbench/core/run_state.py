from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from ilvesbench.models import BenchmarkRunRecord, StepResult


class RunStateService:
    """Small state-transition helper for workflow records."""

    def complete_step(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self.replace_step(record, name, status="completed", details=details, error=None, planned_only=False)

    def mark_planned(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self.replace_step(record, name, status="planned", details=details, error=None, planned_only=True)

    def fail_step(self, record: BenchmarkRunRecord, name: str, error: str) -> BenchmarkRunRecord:
        return self.replace_step(record, name, status="failed", details={}, error=error, planned_only=False)

    def input_required_step(self, record: BenchmarkRunRecord, name: str, details: dict) -> BenchmarkRunRecord:
        return self.replace_step(record, name, status="input_required", details=details, error=None, planned_only=False)

    def replace_step(
        self,
        record: BenchmarkRunRecord,
        name: str,
        *,
        status: str,
        details: dict,
        error: str | None,
        planned_only: bool,
        template_step: StepResult | None = None,
    ) -> BenchmarkRunRecord:
        steps: list[StepResult] = []
        replaced = False
        for step in record.steps:
            if step.name != name:
                steps.append(step)
                continue
            steps.append(
                replace(
                    step,
                    status=status,
                    details=details,
                    error=error,
                    planned_only=planned_only,
                )
            )
            replaced = True
        if not replaced:
            steps.append(
                StepResult(
                    name=name,
                    title=template_step.title if template_step else name.replace("_", " ").title(),
                    status=status,
                    requires_approval=template_step.requires_approval if template_step else False,
                    details=details,
                    error=error,
                    planned_only=planned_only,
                )
            )
        record.steps = steps
        record.updated_at = datetime.now(UTC).isoformat()
        return record
