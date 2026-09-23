"""Runtime secret resolution. Artifacts and app models hold references (`env:NAME`), never
values; values are resolved here, at the last moment, and registered with the run's redactor
by the session before they are used.
"""

import os
from collections.abc import Mapping
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, ref: str) -> str: ...


class EnvSecrets:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, ref: str) -> str:
        scheme, _, name = ref.partition(":")
        if scheme != "env" or not name:
            raise KeyError(f"unsupported secret reference {ref!r}")
        value = self._environ.get(name)
        if not value:
            raise KeyError(f"secret {ref} is not set")  # never echo a value
        return value
