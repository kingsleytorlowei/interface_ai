"""Perception/action port: `observe()`, `resolve(Target)`, `act()`.

Adapters (web via Playwright today; legacy web / desktop by design) translate between a live
surface and the normalized element model in `cua.schema`. Knows nothing about goals, steps,
policies or LLMs. Only `cua.session` may use it.
"""

from .base import ActionTimeout, Pinned, ResolutionError, Resolved, Surface

__all__ = ["ActionTimeout", "Pinned", "ResolutionError", "Resolved", "Surface"]
