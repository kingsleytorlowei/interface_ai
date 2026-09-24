"""App-level state library: recognisable screens and exceptional states of one application,
shared by every capability for that app (and, by design, overridable per tenant).

A state matches when all of its predicates hold. Screens often overlap (an error message is
rendered on top of the search screen it came from), so when several states match, the highest
`precedence` wins; a tie at the top is ambiguous and is treated as an unknown state, never a
guess. Text predicates are case-sensitive substrings of visible text across all frames.

The app library says what a state *is*
(`screen`, `interstitial`, `fatal`); whether a screen is a business outcome is the
capability's call (e.g. `no_results` is just a screen to the app, but `member_not_found` to a
lookup capability).
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .common import Model, SemVer, Slug
from .targets import Target


class HasText(Model):
    kind: Literal["text"] = "text"
    text: str


class HasTextRegex(Model):
    kind: Literal["text_regex"] = "text_regex"
    pattern: str


class HasElement(Model):
    kind: Literal["element"] = "element"
    role: str
    name: str | None = None
    name_contains: str | None = None


class UrlContains(Model):
    kind: Literal["url_contains"] = "url_contains"
    value: str


class TitleContains(Model):
    kind: Literal["title_contains"] = "title_contains"
    value: str


Predicate = Annotated[
    HasText | HasTextRegex | HasElement | UrlContains | TitleContains,
    Field(discriminator="kind"),
]


class ClickRecovery(Model):
    """Dismiss a known interstitial (e.g. an unexpected confirmation dialog)."""

    kind: Literal["click"] = "click"
    target: Target


class RetryRecovery(Model):
    """Transient failure: wait, then restart the capability from its entry point. Replay only
    permits this while every step executed so far is read-only (restarting past a reversible or
    irreversible step could repeat it); otherwise it escalates."""

    kind: Literal["retry"] = "retry"
    wait_ms: int = Field(default=1000, ge=0)


class ReauthenticateRecovery(Model):
    """Session expired: the runtime re-establishes the session (credentials never in artifacts),
    then restarts from the entry point under the same read-only rule as `retry`."""

    kind: Literal["reauthenticate"] = "reauthenticate"


class EscalateRecovery(Model):
    """No automatic recovery: hand the live session to a human."""

    kind: Literal["escalate"] = "escalate"


Recovery = Annotated[
    ClickRecovery | RetryRecovery | ReauthenticateRecovery | EscalateRecovery,
    Field(discriminator="kind"),
]


class StateKind(StrEnum):
    SCREEN = "screen"  # a normal screen; capabilities use these as checkpoints/outcomes
    INTERSTITIAL = "interstitial"  # gets in the way; has a recovery
    FATAL = "fatal"  # the app is broken; stop with a hard failure


class StateSignature(Model):
    description: str = ""
    match: list[Predicate] = Field(min_length=1)
    kind: StateKind = StateKind.SCREEN
    recovery: Recovery | None = None
    precedence: int = 0
    max_recoveries: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _recovery_iff_interstitial(self) -> "StateSignature":
        if (self.kind is StateKind.INTERSTITIAL) != (self.recovery is not None):
            raise ValueError("interstitial states need a recovery; other kinds must not have one")
        return self


SecretRef = Annotated[str, StringConstraints(pattern=r"^env:[A-Z][A-Z0-9_]*$")]


class SignOn(Model):
    """How the runtime establishes a session. Credentials are secret references resolved at run
    time; their values never appear in artifacts, and the LLM never sees or types them."""

    url: str  # template, e.g. "{{env.base_url}}/login"
    username: Target
    password: Target
    submit: Target
    username_secret: SecretRef
    password_secret: SecretRef
    success: list[Predicate] = Field(min_length=1)


class AppModel(Model):
    schema_version: Literal["1"] = "1"
    app_id: Slug
    version: SemVer
    description: str = ""
    # Where work starts once signed on (a template, e.g. "{{env.base_url}}/main.html"):
    # new goals begin here.
    home: str | None = None
    sign_on: SignOn | None = None
    states: dict[Slug, StateSignature]

