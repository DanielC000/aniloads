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
_BOT_ANIMELOADS = os.path.join(_BOT_DIR, "animeloads.py")

# A throwaway temp dir for LOG_DIR / CONFIG_DIR so module import never touches
# /config (or C:\config) and never writes outside the test sandbox.
_TMP = tempfile.mkdtemp(prefix="aniloads-tests-")
os.environ.setdefault("LOG_DIR", _TMP)
os.environ.setdefault("CONFIG_DIR", _TMP)

_app_cache = None
_anibot_cache = None
_animeloads_cache = None


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


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_animeloads_stubs():
    """Stub the heavy/optional third-party deps bot/animeloads.py imports at
    module level (myjdapi, pycryptodome, selenium) so it can be imported
    hermetically on a test host that lacks them installed. Only the
    pure-logic helpers are exercised by tests, so none of these stand-ins are
    ever actually called."""
    if "myjdapi" not in sys.modules:
        _stub_module("myjdapi", Myjdapi=type("Myjdapi", (), {}))
    if "Cryptodome" not in sys.modules:
        cipher = _stub_module("Cryptodome.Cipher", AES=type(
            "AES", (), {"MODE_CBC": 2, "new": staticmethod(lambda *a, **k: None)}))
        crypto = _stub_module("Cryptodome", Cipher=cipher)
    if "selenium" not in sys.modules:
        by = _stub_module("selenium.webdriver.common.by", By=type("By", (), {}))
        common = _stub_module("selenium.webdriver.common", by=by)
        ui = _stub_module("selenium.webdriver.support.ui", WebDriverWait=type("WebDriverWait", (), {}))
        ec = _stub_module("selenium.webdriver.support.expected_conditions")
        support_pkg = _stub_module("selenium.webdriver.support", ui=ui, expected_conditions=ec)
        ff_service = _stub_module("selenium.webdriver.firefox.service", Service=type("Service", (), {}))
        ff_options = _stub_module("selenium.webdriver.firefox.options", Options=type("Options", (), {}))
        ff_pkg = _stub_module("selenium.webdriver.firefox", service=ff_service, options=ff_options)
        cr_service = _stub_module("selenium.webdriver.chrome.service", Service=type("Service", (), {}))
        cr_options = _stub_module("selenium.webdriver.chrome.options", Options=type("Options", (), {}))
        cr_pkg = _stub_module("selenium.webdriver.chrome", service=cr_service, options=cr_options)
        webdriver = _stub_module("selenium.webdriver", Firefox=type("Firefox", (), {}),
                                  Chrome=type("Chrome", (), {}), firefox=ff_pkg, chrome=cr_pkg,
                                  support=support_pkg, common=common)
        _stub_module("selenium", webdriver=webdriver)


def load_animeloads():
    """Import bot/animeloads.py once, with heavy optional deps (selenium,
    myjdapi, pycryptodome) stubbed so it imports hermetically."""
    global _animeloads_cache
    if _animeloads_cache is None:
        if _BOT_DIR not in sys.path:
            sys.path.insert(0, _BOT_DIR)
        _install_animeloads_stubs()
        _animeloads_cache = _load_from_path("aniloads_animeloads", _BOT_ANIMELOADS)
    return _animeloads_cache
