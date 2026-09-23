import importlib

MODULES = [
    "cua.schema", "cua.surface", "cua.policy", "cua.control", "cua.session", "cua.evidence",
    "cua.store", "cua.apps", "cua.discovery", "cua.replay", "cua.operator", "cua.cli",
    "mock_bank",
]


def test_modules_import() -> None:
    for name in MODULES:
        importlib.import_module(name)
