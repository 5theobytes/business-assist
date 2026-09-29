"""Pluggable conversation workflows.

Each workflow implements the same Workflow interface so the FastAPI layer and
the eval harness can swap them via the WORKFLOW env var.
"""
from .base import Workflow, WorkflowSession
from .v1_linear import LinearFunnelWorkflow
from .v2_adaptive import AdaptiveStateMachineWorkflow
from .v3_momtest import MomTestJTBDWorkflow
from .v4_clarity import ClarityWorkflow

WORKFLOWS: dict[str, type[Workflow]] = {
    "v1": LinearFunnelWorkflow,
    "v2": AdaptiveStateMachineWorkflow,
    "v3": MomTestJTBDWorkflow,
    "v4": ClarityWorkflow,
}


def build(name: str, **kwargs) -> Workflow:
    if name not in WORKFLOWS:
        raise ValueError(f"Unknown workflow: {name}. Choices: {sorted(WORKFLOWS)}")
    return WORKFLOWS[name](**kwargs)


__all__ = [
    "Workflow",
    "WorkflowSession",
    "WORKFLOWS",
    "build",
    "LinearFunnelWorkflow",
    "AdaptiveStateMachineWorkflow",
    "MomTestJTBDWorkflow",
    "ClarityWorkflow",
]
