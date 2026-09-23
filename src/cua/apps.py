"""Per-app knowledge shared across capabilities: the state library (known error pages,
interstitials, dialogs) and, by design, per-tenant overrides of a vendor-app base.

State classification is a pure function of an `Observation`, so it is unit-testable and can
re-classify saved evidence after the fact.
"""

import re
from dataclasses import dataclass

from cua.schema import AppModel, Observation, Predicate, StateSignature
from cua.schema.states import HasElement, HasText, HasTextRegex, TitleContains, UrlContains


def predicate_holds(predicate: Predicate, obs: Observation) -> bool:
    match predicate:
        case HasText(text=text):
            return text in obs.text
        case HasTextRegex(pattern=pattern):
            return re.search(pattern, obs.text) is not None
        case HasElement(role=role, name=name, name_contains=contains):
            return any(
                n.role == role
                and (name is None or n.name == name)
                and (contains is None or contains in n.name)
                for n in obs.nodes()
            )
        case UrlContains(value=value):
            return any(value in url for url in [obs.url, *obs.frame_urls])
        case TitleContains(value=value):
            return value in obs.title
    raise AssertionError(f"unhandled predicate {predicate!r}")


def state_matches(signature: StateSignature, obs: Observation) -> bool:
    return all(predicate_holds(p, obs) for p in signature.match)


@dataclass(frozen=True)
class Classification:
    state: str | None  # winning state, or None if nothing matched or the top was a tie
    matched: tuple[str, ...]  # every matching state, highest precedence first

    @property
    def ambiguous(self) -> bool:
        return self.state is None and len(self.matched) > 1


def classify(app: AppModel, obs: Observation) -> Classification:
    matched = sorted(
        (name for name, sig in app.states.items() if state_matches(sig, obs)),
        key=lambda name: -app.states[name].precedence,
    )
    if not matched:
        return Classification(None, ())
    top = app.states[matched[0]].precedence
    winners = [m for m in matched if app.states[m].precedence == top]
    return Classification(winners[0] if len(winners) == 1 else None, tuple(matched))
