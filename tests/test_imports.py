"""Import every module in the muze package.

Catches syntax/indent errors in modules that no unit test imports directly
(shipped exactly such a break in web/server.py once: the full suite was
green while serve crash-looped with IndentationError).
"""

import importlib
import pkgutil

import muze


def test_every_module_imports():
    failures = []
    for m in pkgutil.walk_packages(muze.__path__, prefix="muze."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001 - report, don't crash the walk
            failures.append(f"{m.name}: {type(e).__name__}: {e}")
    assert not failures, "\n".join(failures)
