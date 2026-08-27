"""Load integration modules without importing Home Assistant.

The package __init__ imports homeassistant, which is not a test dependency here,
so a synthetic package namespace is built and only the pure-Python modules are
loaded from it.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "ugreen_powerroam"
PKG_NAME = "ugreen_powerroam_under_test"


def load_module(name: str):
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg

    full = f"{PKG_NAME}.{name}"
    if full in sys.modules:
        return sys.modules[full]

    spec = importlib.util.spec_from_file_location(full, PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
