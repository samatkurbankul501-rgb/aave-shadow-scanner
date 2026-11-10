"""tests/test_scan.py — оффлайн-регрессии на ABI, курсор from_block и полный проход main().

Критично: liquidationCall Aave V3 Pool принимает 5 аргов (receiveAToken);
4-арг ABI давал чужой selector -> estimateGas всегда ревертил -> gas_assumed.
"""
import json
from types import SimpleNamespace

from web3 import Web3

from scanner.scan import POOL_ABI, resolve_from_block, resume_from_block


def _liquidation_call():
    return next(f for f in POOL_ABI if f.get("name") == "liquidationCall")


def test_liquidation_call_abi_matches_pool_signature():
    fn = _liquidation_call()
    assert [i["type"] for i in fn["inputs"]] == ["address", "address", "address", "uint256", "bool"]


def test_liquidation_call_selector_is_canonical_v3():
    c = Web3().eth.contract(address="0x" + "11" * 20, abi=POOL_ABI)
    calldata = c.encode_abi("liquidationCall", args=["0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20, 1_000_000, False])
    canonical = Web3.keccak(text="liquidationCall(address,address,address,uint256,bool)")[:4]
    assert calldata[:10] == "0x" + canonical.hex()  # 0x + 8 hex-цифр selector-а
    assert len(Web3.to_bytes(hexstr=calldata)) == 4 + 32 * 5


def test_resolve_from_block_zero_is_valid():
    assert resolve_from_block(0, 1000) == 0
    assert resolve_from_block(None, 1000) == 1000 - 10000
    assert resolve_from_block(777, 1000) == 777


def test_cursor_resume_no_gap_after_long_downtime():
    # Курсор отстал на 50000 блоков (> head-10000): продолжаем с курсора,
    # а не прыгаем на head-10000 с пропуском блоков.
    safe = 1_000_000
    assert resume_from_block(saved=950_000, args_from_block=None, safe_block=safe) == 950_001
    # Курсор внутри обычного окна — тоже продолжается
    assert resume_from_block(saved=999_000, args_from_block=None, safe_block=safe) == 999_001


def test_explicit_from_block_beats_cursor_and_no_cursor_default():
    safe = 1_000_000
    assert resume_from_block(saved=950_000, args_from_block=777, safe_block=safe) == 777
    assert resume_from_block(saved=None, args_from_block=None, safe_block=safe) == safe - 10000
    # Курсор впереди safe_block (реорг/часы) — не идём назад, дефолтное окно
    assert resume_from_block(saved=safe + 5, args_from_block=None, safe_block=safe) == safe - 10000


def _liq_log(addr_hex: str):
    # data: debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken
    data = (1000).to_bytes(32, "big") + (500).to_bytes(32, "big") + bytes(12) + bytes.fromhex(addr_hex) + bytes(32)
    return {"data": data}


def test_liquidator_from_log_reads_data_word3():
    from scanner.scan import _liquidator_from_log
    addr = "0x" + "ab" * 20
    assert _liquidator_from_log(_liq_log("ab" * 20)) == Web3.to_checksum_address(addr)
    assert _liquidator_from_log({"data": "0x" + _liq_log("ab" * 20)["data"].hex()}) == Web3.to_checksum_address(addr)
    assert _liquidator_from_log({"data": b"\x00" * 32}) is None  # короткая data
    assert _liquidator_from_log({}) is None


def test_liquidator_from_requires_balance_and_allowance(monkeypatch):
    monkeypatch.setattr("scanner.rpc_client._MIN_INTERVAL", 0)
    from scanner.scan import liquidator_from
    good = Web3.to_checksum_address("0x" + "aa" * 20)
    poor = Web3.to_checksum_address("0x" + "bb" * 20)
    asset = Web3.to_checksum_address("0x" + "cc" * 20)
    logs = [_liq_log("bb" * 20), _liq_log("aa" * 20)]  # reversed: good пробуют первым

    class FakeCall:
        def __init__(self, val):
            self.val = val

        def call(self):
            return self.val

    class FakeContract:
        functions = None

        def __init__(self):
            self.functions = self

        def balanceOf(self, a):
            return FakeCall(10**18 if a == good else 0)

        def allowance(self, a, spender):
            return FakeCall(10**30 if a == good else 0)

    class FakeEth:
        @staticmethod
        def get_logs(f):
            return logs

        @staticmethod
        def contract(address=None, abi=None):
            return FakeContract()

    class FakeW3:
        eth = FakeEth()

    cache = {}
    assert liquidator_from(FakeW3(), asset, 1000, 5000, cache) == good
    assert cache[asset] == good
    # Кэш: повторный вызов не ходит в RPC (w3=None не используется)
    assert liquidator_from(None, asset, 1000, 5000, cache) == good
    # Нет подходящего ликвидатора (бедный без баланса) -> None в кэше
    logs[:] = [_liq_log("bb" * 20)]
    cache2 = {}
    assert liquidator_from(FakeW3(), asset, 1000, 5000, cache2) is None
    assert cache2[asset] is None


def test_main_full_pass_offline(monkeypatch, tmp_path):
    """Полный проход main() на фейках: лог, курсор, fallback-газ, цена POL из оракула."""
    import scanner.scan as scan
    from scanner import rpc_client

    monkeypatch.setattr(rpc_client, "_MIN_INTERVAL", 0)
    user = Web3.to_checksum_address("0x" + "11" * 20)
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)
    # hf 0.97, долг $100; резерв < $2000 -> coverable_debt=1.0 (весь долг)
    account_data = (200 * 10**8, 100 * 10**8, 0, 800000, 750000, int(0.97 * 10**18))

    class FakeCall:
        def __init__(self, val):
            self.val = val

        def call(self, block_identifier=None):
            return self.val

    class FakeFunctions:
        @staticmethod
        def getUserAccountData(u):
            return FakeCall(account_data)

    class FakeContract:
        def __init__(self):
            self.functions = FakeFunctions()

    class FakeEth:
        gas_price = 30 * 10**9

        @staticmethod
        def contract(address=None, abi=None):
            return FakeContract()

        @staticmethod
        def get_block(n):
            return SimpleNamespace(hash=SimpleNamespace(hex=lambda: "0xdeadbeef"))

        @staticmethod
        def get_logs(f):
            return []  # ликвидаторов нет -> честный gas_assumed

    class FakeW3:
        eth = FakeEth()

    info = {"liquidationBonus": 5.0, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27, "decimals": 6, "price_in_market_ccy": 10**8}
    coll_info = {"liquidationBonus": 5.0, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27, "decimals": 6, "price_in_market_ccy": 10**8}
    monkeypatch.setattr(scan, "connect", lambda url: FakeW3())
    monkeypatch.setattr(scan, "get_block_number", lambda w3: 1000)
    monkeypatch.setattr(scan, "scan_range", lambda w3, a, b: ({user}, 984))
    monkeypatch.setattr(scan, "load_reserve_cache", lambda w3: ({debt_asset: info, coll_asset: coll_info}, 0.5))
    monkeypatch.setattr(scan, "top_position", lambda w3, u, block_identifier=None: (debt_asset, 100 * 10**6, coll_asset, 200 * 10**6))
    monkeypatch.setattr("sys.argv", ["scan", "--rpc", "x", "--out", str(tmp_path)])

    assert scan.main() == 0
    passes = list(tmp_path.glob("pass_*.json"))
    assert len(passes) == 1
    s = json.loads(passes[0].read_text(encoding="utf-8"))
    assert s["candidates_checked"] == 1
    assert s["liquidatable"] == 1
    assert s["gas_assumed"] == 1
    assert s["pol_price_usd"] == 0.5
    assert s["pol_price_source"] == "oracle"
    assert s["last_ok_block"] == 984
    assert (tmp_path / "cursor.txt").read_text(encoding="utf-8") == "984"
    cand = s["candidates"][0]
    assert cand["block"] == 984
    assert cand["flag"] == "gas_assumed"
    assert cand["bonus_pct"] == 5.0
    # Резерв долга $100 < $2000 -> coverable_debt=1.0 (small-reserve правило),
    # покрывается весь долг $100; net = 100*5% - fee - gas > 0
    assert s["net_positive"] == 1
    rows = [json.loads(line) for line in list(tmp_path.glob("candidates_*.jsonl"))[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["user"] == user
