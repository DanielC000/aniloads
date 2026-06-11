"""Test support: hermetic loaders for web/app.py and bot/anibot.py.

These modules pull in heavy/optional runtime deps (selenium, myjdapi, lxml, ...)
that the test host does not install. We load the target modules by file path and
inject lightweight stand-ins into sys.modules first, so importing them launches
nothing and reaches no network. The pure-logic functions under test only use the
stdlib, so the stubs are never exercised.
"""

import importlib.util
import os
import sys
import tempfile
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_DIR = os.path.join(_REPO_ROOT, "bot")
_WEB_APP = os.path.join(_REPO_ROOT, "web", "app.py")
_BOT_ANIBOT = os.path.join(_BOT_DIR, "anibot.py")

# A throwaway temp dir for LOG_DIR / CONFIG_DIR so module import never touches
# /config (or C:\config) and never writes outside the test sandbox.
_TMP = tempfile.mkdtemp(prefix="aniloads-tests-")
os.environ.setdefault("LOG_DIR", _TMP)
os.environ.setdefault("CONFIG_DIR", _TMP)

_app_cache = None
_anibot_cache = None


def _load_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_app():
    """Import web/app.py once. It is already import-safe (threads under
    __main__, the animeloads import is guarded by try/except)."""
    global _app_cache
    if _app_cache is None:
        _app_cache = _load_from_path("aniloads_web_app", _WEB_APP)
    return _app_cache


def _install_anibot_stubs():
    """Stub the heavy third-party deps anibot.py imports at module level so the
    real (uninstalled) packages aren't required to import the module."""
    if "myjdapi" not in sys.modules:
        sys.modules["myjdapi"] = types.ModuleType("myjdapi")
    if "animeloads" not in sys.modules:
        al = types.ModuleType("animeloads")
        al.animeloads = type("animeloads", (), {})
        al.ALLinkExtractionException = type("ALLinkExtractionException", (Exception,), {})
        sys.modules["animeloads"] = al


def load_anibot():
    """Import bot/anibot.py once. The CLI dispatch is guarded by
    __name__ == '__main__', so importing it launches no bot."""
    global _anibot_cache
    if _anibot_cache is None:
        if _BOT_DIR not in sys.path:
            sys.path.insert(0, _BOT_DIR)  # let `from tvdb import TVDBClient` resolve
        _install_anibot_stubs()
        _anibot_cache = _load_from_path("aniloads_anibot", _BOT_ANIBOT)
    return _anibot_cache
