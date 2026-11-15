"""tests/test_scan.py — оффлайн-регрессии на ABI, курсор from_block и полный проход main().

Критично: liquidationCall Aave V3 Pool принимает 5 аргов (receiveAToken);
4-арг ABI давал чужой selector -> estimateGas всегда ревертил -> gas_assumed.
"""
import json
from types import SimpleNamespace

import pytest
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
            return SimpleNamespace(hash=SimpleNamespace(hex=lambda: "0xdeadbeef"), timestamp=1700000000)

        @staticmethod
        def get_logs(f):
            return []  # ликвидаторов нет -> честный gas_assumed

    class FakeW3:
        eth = FakeEth()

    info = {"liquidationBonus": 6.3, "liquidationBonusGross": 7.0, "liquidationProtocolFee": 1000, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27, "decimals": 6, "price_in_market_ccy": 10**8, "isActive": True, "isPaused": False}
    coll_info = {"liquidationBonus": 4.5, "liquidationBonusGross": 5.0, "liquidationProtocolFee": 1000, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27, "decimals": 6, "price_in_market_ccy": 10**8, "isActive": True, "isPaused": False}
    monkeypatch.setattr(scan, "connect", lambda url: FakeW3())
    monkeypatch.setattr(scan, "get_block_number", lambda w3: 1000)
    monkeypatch.setattr(scan, "scan_range", lambda w3, a, b: ({user}, 984))
    monkeypatch.setattr(scan, "load_reserve_cache", lambda w3: ({debt_asset: info, coll_asset: coll_info}, 0.5))
    monkeypatch.setattr(scan, "top_position", lambda w3, u, reserve_cache=None, block_identifier=None: (debt_asset, 100 * 10**6, coll_asset, 200 * 10**6, 0))
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
    assert s["swap_cost_pct_assumed"] == scan.SWAP_COST_PCT_ASSUMED
    assert s["liq_fee_unreadable"] == 0
    assert s["last_ok_block"] == 984
    assert (tmp_path / "cursor.txt").read_text(encoding="utf-8") == "984"
    cand = s["candidates"][0]
    assert cand["block"] == 984
    assert cand["flag"] == "gas_assumed"
    assert cand["bonus_pct"] == 4.5  # net-бонус КОЛЛАТЕРАЛА (не debt-резерва 6.3)
    assert cand["liquidation_protocol_fee_bps"] == 1000
    assert cand["swap_cost_pct"] == scan.SWAP_COST_PCT_ASSUMED
    # Резерв долга $100 < $2000 -> coverable_debt=1.0 (small-reserve правило),
    # покрывается весь долг $100; net = 100*5% - fee - gas > 0
    assert s["net_positive"] == 1
    rows = [json.loads(line) for line in list(tmp_path.glob("candidates_*.jsonl"))[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["user"] == user
    assert s["watchlist_persisted"] == 1
    assert (tmp_path / "watchlist.txt").read_text(encoding="utf-8").strip() == user


def test_watchlist_helpers_roundtrip(tmp_path):
    from scanner.scan import load_watchlist, save_watchlist
    p = tmp_path / "watchlist.txt"
    assert load_watchlist(p) == set()  # файла нет
    save_watchlist({"0xb", "0xa"}, p)
    assert load_watchlist(p) == {"0xa", "0xb"}
    assert p.read_text(encoding="utf-8") == "0xa\n0xb\n"  # детерминированный порядок
    save_watchlist(set(), p)
    assert load_watchlist(p) == set()
    p.write_text("", encoding="utf-8")
    assert load_watchlist(p) == set()


def _two_pass_fakes(monkeypatch, tmp_path, user_a, user_b):
    """Общие фейки для тестов персистентного вотч-листа: A с долгом (здоров), B без долга."""
    import scanner.scan as scan
    from scanner import rpc_client

    monkeypatch.setattr(rpc_client, "_MIN_INTERVAL", 0)
    healthy = (200 * 10**8, 100 * 10**8, 0, 800000, 750000, int(1.5 * 10**18))
    no_debt = (0, 0, 0, 0, 0, 10**30)

    class FakeCall:
        def __init__(self, val):
            self.val = val

        def call(self, block_identifier=None):
            return self.val

    class FakeFunctions:
        @staticmethod
        def getUserAccountData(u):
            return FakeCall(healthy if u == user_a else no_debt)

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
            return SimpleNamespace(hash=SimpleNamespace(hex=lambda: "0xdeadbeef"), timestamp=1700000000)

        @staticmethod
        def get_logs(f):
            return []

    class FakeW3:
        eth = FakeEth()

    monkeypatch.setattr(scan, "connect", lambda url: FakeW3())
    monkeypatch.setattr(scan, "get_block_number", lambda w3: 1006)
    monkeypatch.setattr(scan, "load_reserve_cache", lambda w3: ({}, None))
    monkeypatch.setattr("sys.argv", ["scan", "--rpc", "x", "--out", str(tmp_path)])
    return scan


def test_main_watchlist_carried_and_zero_debt_pruned(monkeypatch, tmp_path):
    """#23: проход 2 без новых событий перепроверяет персистентного юзера;
    бездолговой B вычищен из вотч-листа после прохода 1."""
    user_a = Web3.to_checksum_address("0x" + "11" * 20)
    user_b = Web3.to_checksum_address("0x" + "22" * 20)
    scan = _two_pass_fakes(monkeypatch, tmp_path, user_a, user_b)
    discovery = [({user_a, user_b}, 984), (set(), 990)]
    monkeypatch.setattr(scan, "scan_range", lambda w3, a, b: discovery.pop(0))

    assert scan.main() == 0
    p1 = list(tmp_path.glob("pass_*.json"))
    assert len(p1) == 1
    s1 = json.loads(p1[0].read_text(encoding="utf-8"))
    assert s1["candidates_checked"] == 2
    assert s1["watchlist_persisted"] == 1  # B без долга вычищен
    assert (tmp_path / "watchlist.txt").read_text(encoding="utf-8").strip() == user_a

    assert scan.main() == 0
    p2 = [p for p in tmp_path.glob("pass_*.json") if p not in p1]
    assert len(p2) == 1
    s2 = json.loads(p2[0].read_text(encoding="utf-8"))
    assert s2["candidates_checked"] == 1  # A перепроверен без новых событий
    assert s2["watchlist_carried"] == 1
    assert s2["watchlist_persisted"] == 1


def test_main_rpc_down_saves_watchlist(monkeypatch, tmp_path):
    """#23: RPC_DOWN посреди опроса не теряет обнаруженных юзеров."""
    user = Web3.to_checksum_address("0x" + "11" * 20)
    scan = _two_pass_fakes(monkeypatch, tmp_path, user, user)

    class BoomFunctions:
        @staticmethod
        def getUserAccountData(u):
            raise SystemExit("RPC_DOWN")

    class BoomContract:
        def __init__(self):
            self.functions = BoomFunctions()

    class BoomEth:
        gas_price = 30 * 10**9

        @staticmethod
        def contract(address=None, abi=None):
            return BoomContract()

        @staticmethod
        def get_block(n):
            return SimpleNamespace(hash=SimpleNamespace(hex=lambda: "0xdeadbeef"), timestamp=1700000000)

        @staticmethod
        def get_logs(f):
            return []

    class BoomW3:
        eth = BoomEth()

    monkeypatch.setattr(scan, "connect", lambda url: BoomW3())
    monkeypatch.setattr(scan, "scan_range", lambda w3, a, b: ({user}, 984))

    with pytest.raises(SystemExit, match="RPC_DOWN"):
        scan.main()
    assert (tmp_path / "cursor.txt").read_text(encoding="utf-8") == "984"
    assert user in (tmp_path / "watchlist.txt").read_text(encoding="utf-8")


def _candidate_pass(monkeypatch, tmp_path, account_data, info, coll_info, pos, emode_bitmap=0b100):
    """Фейки полного прохода с одним кандидатом; eMode-геттеры отвечают категорией 1%."""
    import scanner.scan as scan
    from scanner import rpc_client

    monkeypatch.setattr(rpc_client, "_MIN_INTERVAL", 0)
    user = Web3.to_checksum_address("0x" + "11" * 20)
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)

    class FakeCall:
        def __init__(self, val):
            self.val = val

        def call(self, block_identifier=None):
            return self.val

    class FakeFunctions:
        @staticmethod
        def getUserAccountData(u):
            return FakeCall(account_data)

        @staticmethod
        def getEModeCategoryData(cid):
            return FakeCall((9800, 9850, 10100, "0x" + "00" * 20, "stable"))

        @staticmethod
        def getEModeCategoryCollateralBitmap(cid):
            return FakeCall(emode_bitmap)

        @staticmethod
        def getLiquidationGracePeriod(asset):
            return FakeCall(0)

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
            return SimpleNamespace(hash=SimpleNamespace(hex=lambda: "0xdeadbeef"), timestamp=1700000000)

        @staticmethod
        def get_logs(f):
            return []

    class FakeW3:
        eth = FakeEth()

    monkeypatch.setattr(scan, "connect", lambda url: FakeW3())
    monkeypatch.setattr(scan, "get_block_number", lambda w3: 1000)
    monkeypatch.setattr(scan, "scan_range", lambda w3, a, b: ({user}, 984))
    monkeypatch.setattr(scan, "load_reserve_cache", lambda w3: ({debt_asset: info, coll_asset: coll_info}, 0.5))
    monkeypatch.setattr(scan, "top_position", lambda w3, u, reserve_cache=None, block_identifier=None: pos)
    monkeypatch.setattr("sys.argv", ["scan", "--rpc", "x", "--out", str(tmp_path)])
    assert scan.main() == 0
    return json.loads(list(tmp_path.glob("pass_*.json"))[0].read_text(encoding="utf-8"))


def _info(gross=7.0, net=6.3, rid=1):
    return {"liquidationBonus": net, "liquidationBonusGross": gross, "liquidationProtocolFee": 1000,
            "variableBorrowIndex": 10**27, "liquidityIndex": 10**27, "decimals": 6,
            "price_in_market_ccy": 10**8, "isActive": True, "isPaused": False, "id": rid}


def test_main_emode_bonus_applied(monkeypatch, tmp_path):
    """#21: eMode-категория 1 (коллатерал в bitmap) -> бонус категории 1% gross,
    net 0.9% после fee 1000 bps, а не резервные 4.5%."""
    account_data = (200 * 10**8, 100 * 10**8, 0, 800000, 750000, int(0.97 * 10**18))
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)
    pos = (debt_asset, 100 * 10**6, coll_asset, 200 * 10**6, 1)
    s = _candidate_pass(monkeypatch, tmp_path, account_data, _info(), _info(5.0, 4.5, rid=2), pos)
    cand = s["candidates"][0]
    assert cand["bonus_pct"] == pytest.approx(0.9)
    assert cand["emode_category"] == 1
    assert s["emode_unreadable"] == 0
    assert s["net_positive"] == 1


def test_main_emode_not_in_bitmap_uses_reserve_bonus(monkeypatch, tmp_path):
    account_data = (200 * 10**8, 100 * 10**8, 0, 800000, 750000, int(0.97 * 10**18))
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)
    pos = (debt_asset, 100 * 10**6, coll_asset, 200 * 10**6, 1)
    # бит 2 не установлен -> коллатерал вне категории -> резервный net 4.5%
    s = _candidate_pass(monkeypatch, tmp_path, account_data, _info(), _info(5.0, 4.5, rid=2), pos, emode_bitmap=0b010)
    assert s["candidates"][0]["bonus_pct"] == pytest.approx(4.5)


def test_main_paused_reserve_skipped(monkeypatch, tmp_path):
    """#26: paused резерв — ликвидация ревертит, кандидат не логируется."""
    account_data = (200 * 10**8, 100 * 10**8, 0, 800000, 750000, int(0.97 * 10**18))
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)
    pos = (debt_asset, 100 * 10**6, coll_asset, 200 * 10**6, 0)
    paused = _info()
    paused["isPaused"] = True
    s = _candidate_pass(monkeypatch, tmp_path, account_data, paused, _info(5.0, 4.5, rid=2), pos)
    assert s["liquidatable"] == 1
    assert s["reserve_blocked"] == 1
    assert s["candidates"] == []


def test_main_dust_shrinks_cover(monkeypatch, tmp_path):
    """#25: HF 0.99, долг резерва $2600, общий $5000 -> cap $2500, но остаток
    $100 < $1000 (MustNotLeaveDust) -> исполнимо $1600, прибыль считается с него."""
    account_data = (10000 * 10**8, 5000 * 10**8, 0, 800000, 750000, int(0.99 * 10**18))
    debt_asset = Web3.to_checksum_address("0x" + "22" * 20)
    coll_asset = Web3.to_checksum_address("0x" + "33" * 20)
    pos = (debt_asset, 2600 * 10**6, coll_asset, 10000 * 10**6, 0)
    s = _candidate_pass(monkeypatch, tmp_path, account_data, _info(), _info(5.0, 4.5, rid=2), pos)
    cand = s["candidates"][0]
    assert cand["debt_to_cover_usd"] == pytest.approx(1600.0)
    assert cand["cover_limit"] == "dust"
    assert cand["net_profit_usd"] > 0
