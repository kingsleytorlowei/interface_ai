"""LLM-driven observe -> decide -> act loop, and the recorder that turns executed actions into
semantic, parameterized steps. The only place an LLM is used.
"""

from .contract import (
    ClaudeContractProposer,
    ContractProposal,
    ContractProposer,
    ProposalError,
    sensitive_looking,
)
from .goal import Goal, GoalInput
from .loop import DiscoveryResult, discover
from .planner import ClaudePlanner, Planner, ScriptedPlanner
from .recorder import Recorder, RecordingError, prune_strategies

__all__ = ["ClaudeContractProposer", "ClaudePlanner", "ContractProposal", "ContractProposer",
           "DiscoveryResult", "Goal", "GoalInput", "Planner", "ProposalError", "Recorder",
           "RecordingError", "ScriptedPlanner", "discover", "prune_strategies",
           "sensitive_looking"]
