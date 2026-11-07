from arb import logbuf
from arb.maintenance import decide


def test_decide():
    assert decide("0.2.0", "0.2.0", False) == "keep"
    assert decide("0.2.0", "0.2.0", True) == "keep"
    assert decide("0.1.0", "0.2.0", False) == "wipe"
    assert decide(None, "0.2.0", False) == "wipe"
    assert decide("0.1.0", "0.2.0", True) == "keep_protected"


def test_check_auth():
    from arb.api import check_auth
    assert check_auth("me", "k" * 20, {"X-API-Id": "me", "X-API-Key": "k" * 20})
    assert not check_auth("me", "k" * 20, {"X-API-Id": "me", "X-API-Key": "x"})
    assert not check_auth("me", "k" * 20, {"X-API-Id": "you", "X-API-Key": "k" * 20})
    assert not check_auth("me", "k" * 20, {})
    assert not check_auth("", "", {"X-API-Id": "", "X-API-Key": ""})   # API non configurée


def test_logbuf_tail():
    import logging
    logbuf.install()
    lg = logging.getLogger("t")
    lg.setLevel(logging.INFO)
    lg.info("alpha")
    lg.warning("beta")
    assert [e["msg"] for e in logbuf.tail(10, "WARNING", "bet")] == ["beta"]
    last = logbuf.tail(1)[0]["id"]
    assert logbuf.tail(10, "INFO", after_id=last) == []
