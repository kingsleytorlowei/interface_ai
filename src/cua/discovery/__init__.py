"""LLM-driven observe -> decide -> act loop, and the recorder that turns executed actions into
semantic, parameterized steps. The only place an LLM is used.
"""

from .goal import Goal, GoalInput
from .loop import DiscoveryResult, discover
from .planner import ClaudePlanner, Planner, ScriptedPlanner
from .recorder import Recorder, RecordingError

__all__ = ["ClaudePlanner", "DiscoveryResult", "Goal", "GoalInput", "Planner", "Recorder",
           "RecordingError", "ScriptedPlanner", "discover"]
