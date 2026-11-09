"""tests/test_discovery.py — offline-регрессии discovery.

- _user_from_log берёт onBehalfOf из topics[2] (позиция держится на нём).
- Курсор: save/load roundtrip, битый файл -> None.
"""
import pytest
from web3 import Web3

from scanner.discovery import _user_from_log, load_cursor, save_cursor


def _log(topics):
    return {"topics": topics}


def test_user_from_log_reads_on_behalf_of_from_topics2():
    addr = "0xAbC0000000000000000000000000000000000001"
    padded = bytes.fromhex("000000000000000000000000" + addr[2:])
    log = _log([b"\x00" * 32, b"\x00" * 32, padded])
    assert _user_from_log(log) == Web3.to_checksum_address(addr)


def test_user_from_log_rejects_short_topics():
    assert _user_from_log(_log([b"\x00" * 32])) is None
    assert _user_from_log({"topics": "not a list"}) is None


def test_cursor_roundtrip_and_bad_file(tmp_path):
    p = tmp_path / "cursor.txt"
    assert load_cursor(p) is None  # файла нет
    save_cursor(12345, p)
    assert load_cursor(p) == 12345
    p.write_text("not a number", encoding="utf-8")
    assert load_cursor(p) is None  # битый файл


def test_scan_range_adaptive_shrink_covers_all_blocks_no_gaps(monkeypatch):
    """Адаптивный батч: при ошибке широкого диапазона режем пополам и не теряем блоки.

    last_ok должен указывать на фактически покрытый конец, а не на запрошенный.
    """
    monkeypatch.setattr("scanner.rpc_client._MIN_INTERVAL", 0)  # без throttle в тесте
    from scanner import discovery

    calls = []

    class FakeEth:
        @staticmethod
        def get_logs(f):
            calls.append((f["fromBlock"], f["toBlock"]))
            if f["toBlock"] - f["fromBlock"] + 1 > 50:
                raise ValueError("simulated range limit")
            return [{"topics": [b"\x00" * 32, b"\x00" * 32, bytes.fromhex("000000000000000000000000" + "aa" * 20)]}]

    class FakeW3:
        eth = FakeEth()

    users, last_ok = discovery.scan_range(FakeW3(), 0, 1000, batch=200)
    assert last_ok == 1000
    assert len(users) == 1

    ok = sorted(set(c for c in calls if c[1] - c[0] + 1 <= 50))
    assert ok[0][0] == 0 and ok[-1][1] == 1000
    for prev, nxt in zip(ok, ok[1:]):
        assert nxt[0] == prev[1] + 1  # цепочка без пропусков блоков


def test_scan_range_mixed_topic_coverage_monotone(monkeypatch):
    """Mixed topic coverage: covered_end монотонно, курсор не идёт назад.

    Borrow покрыл батч целиком, Supply сузился на второй половине.
    Курсору обязан оставаться на фактическом covered_end.
    """
    monkeypatch.setattr("scanner.rpc_client._MIN_INTERVAL", 0)
    from scanner import discovery

    def fake_get(w3, cur, end, pool_addr, topic):
        if topic == discovery.TOPIC_SUPPLY and end - cur + 1 > 200:
            return [], cur + 99
        return [], end

    monkeypatch.setattr(discovery, "_get_logs", fake_get)
    users, last_ok = discovery.scan_range(object(), 0, 300, batch=200)
    assert last_ok == 300
    assert users == set()
