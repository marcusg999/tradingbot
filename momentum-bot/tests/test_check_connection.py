import importlib.util
import os

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                       "check_connection.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_connection", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mask_secret_hides_middle():
    m = _load()
    assert m.mask_secret("PKABCD1234EFGH5678") == "PKAB…5678"
    # Never reveals the full value.
    assert "ABCD1234EFGH" not in m.mask_secret("PKABCD1234EFGH5678")


def test_mask_secret_short_values_fully_masked():
    m = _load()
    assert m.mask_secret("abc") == "***"
    assert m.mask_secret("12345678") == "*" * 8


def test_mask_secret_empty():
    m = _load()
    assert m.mask_secret("") == "(empty)"
