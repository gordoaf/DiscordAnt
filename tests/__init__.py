"""Offline tests always use disposable app data, including with real Playwright."""
import atexit
import importlib.util
import os
import sys
import tempfile
import types

_data = tempfile.TemporaryDirectory(prefix="discordant-tests-")
os.environ["LOCALAPPDATA"] = _data.name
sys.dont_write_bytecode = True
atexit.register(_data.cleanup)
if importlib.util.find_spec("playwright") is None:
    package = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    def no_browser(*args, **kwargs):
        raise AssertionError("Offline tests must not start a real browser")
    api.async_playwright = no_browser
    package.async_api = api
    sys.modules["playwright"] = package
    sys.modules["playwright.async_api"] = api
