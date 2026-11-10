"""tests/test_rpc_client.py — 429/backoff, timeout-ретрай, chain_id, точность _is_429."""
from types import SimpleNamespace

import pytest

import scanner.rpc_client as rc


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(rc, "_MIN_INTERVAL", 0)
    monkeypatch.setattr(rc, "_rl_hits_total", 0)
    monkeypatch.setattr(rc, "_rl_hits_consec", 0)
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)


def _fn_raising(messages, result=42):
    seq = list(messages)

    def fn():
        if seq:
            raise ValueError(seq.pop(0))
        return result
    return fn


def test_is_429_no_false_positive_on_block_numbers():
    assert rc._is_429("429 client error: too many requests")
    assert rc._is_429("ratelimit exceeded")
    assert rc._is_429("rate limit exceeded")
    assert not rc._is_429("block 429123 not found")
    assert not rc._is_429("execution reverted")


def test_429_backoff_then_success():
    fn = _fn_raising(["429 too many", "429 too many"])
    assert rc.rpc_call(None, fn) == 42
    assert rc.get_rl_hits() == 2


def test_429_three_consecutive_rpc_down():
    with pytest.raises(SystemExit, match="RPC_DOWN"):
        rc.rpc_call(None, _fn_raising(["429"] * 5))


def test_timeout_retry_once_then_success():
    assert rc.rpc_call(None, _fn_raising(["request timeout"])) == 42


def test_timeout_twice_rpc_down():
    with pytest.raises(SystemExit, match="RPC_DOWN"):
        rc.rpc_call(None, _fn_raising(["timeout", "timed out"]))


def test_timeout_retry_survives_prior_429():
    # Регрессия: 429-ретрай не должен съедать единственный timeout-ретрай
    assert rc.rpc_call(None, _fn_raising(["429 too many", "timeout"])) == 42


def test_other_errors_propagate():
    with pytest.raises(ValueError, match="execution reverted"):
        rc.rpc_call(None, _fn_raising(["execution reverted"]))


def test_verify_chain_polygon_ok():
    rc.verify_chain(SimpleNamespace(eth=SimpleNamespace(chain_id=137)))


def test_verify_chain_wrong_network_rejected():
    with pytest.raises(SystemExit, match="WRONG_CHAIN"):
        rc.verify_chain(SimpleNamespace(eth=SimpleNamespace(chain_id=1)))