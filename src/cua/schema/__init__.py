"""Pure data contracts shared by every other module.

Capability artifact, semantic `Target`s, the app state library and run results. No I/O and no
dependency on any other `cua` module or third-party runtime (browser, LLM) — enforced by
import-linter.
"""

from .capability import (
    Action,
    AppRef,
    Capability,
    Checkpoint,
    Click,
    Entry,
    Extract,
    Fill,
    InputSpec,
    Navigate,
    OutcomeSpec,
    OutputSpec,
    ParamType,
    Press,
    Provenance,
    Select,
    Status,
    Step,
    SuccessCondition,
    check_against_app,
)
from .common import Risk, Sensitivity, render_template
from .observation import ElementInfo, Observation, UINode
from .overlay import BaseRef, CapabilityOverlay, OverlayProvenance, apply_overlay
from .results import (
    Aborted,
    BusinessOutcome,
    DriftSignal,
    Failure,
    FailureCategory,
    HumanAction,
    Intervention,
    RecoveryRecord,
    RunResult,
    Success,
)
from .stability import StabilityReport, StabilityRun, Verdict
from .states import AppModel, Predicate, Recovery, SignOn, StateKind, StateSignature
from .targets import Fingerprint, FrameSelector, Strategy, Target

__all__ = [
    "Aborted", "Action", "AppModel", "AppRef", "BaseRef", "BusinessOutcome", "Capability",
    "CapabilityOverlay", "Checkpoint",
    "Click", "DriftSignal", "ElementInfo", "Entry", "Extract", "Failure", "FailureCategory",
    "Fill", "Fingerprint", "FrameSelector", "HumanAction", "InputSpec", "Intervention",
    "Navigate", "Observation", "OutcomeSpec", "OutputSpec", "OverlayProvenance", "ParamType",
    "Predicate", "Press",
    "Provenance", "Recovery", "RecoveryRecord", "Risk", "RunResult", "Select", "Sensitivity",
    "SignOn", "StabilityReport", "StabilityRun", "StateKind", "StateSignature", "Status", "Step",
    "Strategy", "Success",
    "SuccessCondition", "Target", "UINode", "Verdict", "apply_overlay", "check_against_app",
    "render_template",
]
