import logging

from logger import _HumanFormatter, _JsonFormatter


def make_record(msg="cycle", extra=None):
    rec = logging.LogRecord(
        name="momentum-bot", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None)
    if extra is not None:
        rec.extra_fields = extra
    return rec


def test_human_formatter_appends_structured_fields():
    fmt = _HumanFormatter("%(levelname)s | %(message)s")
    out = fmt.format(make_record(extra={"equity": 10000.0, "exposure": 0.0}))
    assert "cycle" in out
    assert "equity=10000.0" in out and "exposure=0.0" in out


def test_human_formatter_plain_message_unchanged():
    fmt = _HumanFormatter("%(levelname)s | %(message)s")
    assert fmt.format(make_record()) == "INFO | cycle"


def test_json_formatter_includes_fields():
    fmt = _JsonFormatter()
    out = fmt.format(make_record(extra={"symbol": "BTC/USD"}))
    assert '"symbol": "BTC/USD"' in out and '"msg": "cycle"' in out
