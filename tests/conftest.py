import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def grab(tmp_path, monkeypatch):
    module = load("grab", "grab.py")
    monkeypatch.setattr(module, "STATE_PATH", str(tmp_path / "grab-state.json"))
    monkeypatch.setattr(module, "LOCK_PATH", str(tmp_path / "grab.lock"))
    return module


@pytest.fixture
def subs(tmp_path, monkeypatch):
    module = load("subs_translate", "subs-translate.py")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(module, "LLM_KEY_FILE", str(tmp_path / "llm-api-key"))
    monkeypatch.setattr(module, "LLM_MODEL", "")
    return module
