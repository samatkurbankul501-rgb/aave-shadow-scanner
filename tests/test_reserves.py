"""tests/test_reserves.py — тесты scanner.reserves + logger dedup.

reserves: проверка _reserve_info (множитель bps -> %) и топографии debtAsset.
logger: дедуп по user внутри одного прохода.
"""
import json
from types import SimpleNamespace

import pytest

from scanner.reserves import _reserve_info, load_reserve_cache
from scanner.logger import log_entry, summary, reset_pass
from pathlib import Path
import tempfile


def _fake_reserve(bonus_raw=10500, symbol="USDC"):
    # Строим минимальный dict-формат AggregatedReserveData
    return {
        "underlyingAsset": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "symbol": symbol,
        "decimals": 6,
        "reserveLiquidationBonus": bonus_raw,
        "variableBorrowIndex": 10**27,
        "liquidityIndex": 10**27,
        "priceInMarketReferenceCurrency": 100_000_000,
        "isActive": True,
        "isPaused": False,
        "borrowingEnabled": True,
    }


def test_reserve_info_bonus_multiplier_bps_to_pct():
    # Aave V3: reserveLiquidationBonus — множитель в bps (10000 = 100%, 10500 = +5%)
    info = _reserve_info(_fake_reserve(10500))
    assert info["liquidationBonus"] == pytest.approx(5.0)
    info2 = _reserve_info(_fake_reserve(11000, "WMATIC"))
    assert info2["liquidationBonus"] == pytest.approx(10.0)
    info3 = _reserve_info(_fake_reserve(10000))
    assert info3["liquidationBonus"] == pytest.approx(0.0)
    assert info["variableBorrowIndex"] == 10**27  # нужен для scaledDebt -> реальный долг
    assert info["liquidityIndex"] == 10**27  # нужен для scaledAToken -> реальный коллатерал (closeFactor)


def test_logger_dedup():
    reset_pass()
    tmp = Path(tempfile.mkdtemp())
    r1 = log_entry({"user": "0x1", "hf": 0.97, "ts": 1, "net_profit_usd": 5.0}, tmp)
    r2 = log_entry({"user": "0x1", "hf": 0.98, "ts": 2, "net_profit_usd": 3.0}, tmp)
    # второй проход — тот же user, dedup внутри reset_pass цикла
    assert r2["hf"] == 0.98
    assert r2["ts"] == 2
    assert r2["net_profit_usd"] == 3.0  # свежий net, не только hf/ts
    # JSONL: ровно одна строка на user, со СВЕЖИМ hf/ts (спека: оставляем свежий HF)
    files = list(tmp.glob("candidates_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["hf"] == 0.98
    assert rows[0]["ts"] == 2
    assert rows[0]["net_profit_usd"] == 3.0  # файл не расходится со сводным JSON
    reset_pass()
    # после reset dedup сбрасывается
    r3 = log_entry({"user": "0x1", "hf": 0.90, "ts": 3, "net_profit_usd": 1.0}, tmp)
    assert r3["hf"] == 0.90


def test_logger_no_same_second_collision():
    # Регрессия: два прохода в одну секунду — РАЗНЫЕ файлы, по одной строке на user
    reset_pass()
    tmp = Path(tempfile.mkdtemp())
    log_entry({"user": "0x1", "hf": 0.9, "ts": 1}, tmp)
    reset_pass()
    log_entry({"user": "0x1", "hf": 0.8, "ts": 1}, tmp)
    files = list(tmp.glob("candidates_*.jsonl"))
    assert len(files) == 2
    for f in files:
        rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
    reset_pass()


def test_top_position_collateral_requires_user_flag(monkeypatch):
    from scanner import reserves

    a = "0x" + "aa" * 20
    b = "0x" + "bb" * 20
    user_res = [
        # больший aTokenBalance, но флаг коллатерала ВЫКЛ — не коллатерал
        {"underlyingAsset": a, "scaledATokenBalance": 100, "usageAsCollateralEnabledOnUser": False, "scaledVariableDebt": 10},
        {"underlyingAsset": b, "scaledATokenBalance": 50, "usageAsCollateralEnabledOnUser": True, "scaledVariableDebt": 0},
    ]
    monkeypatch.setattr(reserves, "rpc_call", lambda *args, **kwargs: (user_res, 0))

    class FakeFunctions:
        @staticmethod
        def getUserReservesData(provider, user):
            return SimpleNamespace(call=lambda **kw: None)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    pos = reserves.top_position(FakeW3(), "0xuser")
    assert pos == (a, 10, b, 50, 0)  # долг — a, коллатерал — b (с флагом), scaled 50, eMode 0


def test_top_position_usd_ranking_beats_scaled(monkeypatch):
    """#24: топ-актив по USD, не по scaled-балансу (разные decimals/цены).

    USDC: 2000 токенов (scaled 2e9, 6 dec, $1) = $2000.
    WETH: 0.5 токена (scaled 5e17, 18 dec, $3000) = $1500.
    По scaled выиграл бы WETH — по USD правильно USDC.
    """
    from scanner import reserves

    usdc = "0x" + "aa" * 20
    weth = "0x" + "bb" * 20
    user_res = [
        {"underlyingAsset": weth, "scaledATokenBalance": 5 * 10**17, "usageAsCollateralEnabledOnUser": True, "scaledVariableDebt": 5 * 10**17},
        {"underlyingAsset": usdc, "scaledATokenBalance": 2000 * 10**6, "usageAsCollateralEnabledOnUser": True, "scaledVariableDebt": 2000 * 10**6},
    ]
    monkeypatch.setattr(reserves, "rpc_call", lambda *args, **kwargs: (user_res, 0))

    class FakeFunctions:
        @staticmethod
        def getUserReservesData(provider, user):
            return SimpleNamespace(call=lambda **kw: None)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    cache = {
        usdc: {"decimals": 6, "price_in_market_ccy": 10**8, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27},
        weth: {"decimals": 18, "price_in_market_ccy": 3000 * 10**8, "variableBorrowIndex": 10**27, "liquidityIndex": 10**27},
    }
    pos = reserves.top_position(FakeW3(), "0xuser", reserve_cache=cache)
    assert pos[0] == usdc and pos[1] == 2000 * 10**6  # топ-долг по USD
    assert pos[2] == usdc and pos[3] == 2000 * 10**6  # топ-коллатерал по USD
    # Без кэша — legacy-ранжирование по scaled (WETH)
    pos_legacy = reserves.top_position(FakeW3(), "0xuser")
    assert pos_legacy[0] == weth


def test_top_position_returns_emode_category(monkeypatch):
    """#21: категория юзера — второй возврат getUserReservesData, не выбрасывается."""
    from scanner import reserves

    a = "0x" + "aa" * 20
    user_res = [{"underlyingAsset": a, "scaledATokenBalance": 100, "usageAsCollateralEnabledOnUser": True, "scaledVariableDebt": 10}]
    monkeypatch.setattr(reserves, "rpc_call", lambda *args, **kwargs: (user_res, 3))

    class FakeFunctions:
        @staticmethod
        def getUserReservesData(provider, user):
            return SimpleNamespace(call=lambda **kw: None)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    pos = reserves.top_position(FakeW3(), "0xuser")
    assert pos[4] == 3


def test_reserve_info_is_paused():
    r = _fake_reserve()
    r["isPaused"] = True
    assert _reserve_info(r)["isPaused"] is True
    assert _reserve_info(_fake_reserve())["isPaused"] is False


def _cache_fake_w3(row, base, cfg=None, fee_reverts=False):
    class FakeFunctions:
        def getReservesData(self, provider):
            return SimpleNamespace(call=lambda: ([row], base))

        def getConfiguration(self, a):
            if fee_reverts:
                def boom():
                    raise ValueError("revert")
                return SimpleNamespace(call=boom)
            return SimpleNamespace(call=lambda: cfg)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    return FakeW3()


def test_load_reserve_cache_net_bonus_after_protocol_fee(monkeypatch):
    from scanner import reserves
    asset = "0x" + "aa" * 20
    row = _fake_reserve(10500)  # gross +5%
    row["underlyingAsset"] = asset
    base = (10**8, 10**8, 50_000_000, 8)  # pol_usd = 0.5
    cfg = (10500 << 32) | (1000 << 152)  # bonus 10500 (bits 32-47), fee 1000 bps (bits 152-167)
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache, pol_usd = reserves.load_reserve_cache(_cache_fake_w3(row, base, cfg=cfg))
    info = cache[asset]
    assert info["liquidationBonusGross"] == pytest.approx(5.0)
    assert info["liquidationProtocolFee"] == 1000
    assert info["liquidationBonus"] == pytest.approx(4.5)  # net = 5 * (1 - 1000/10000)
    assert pol_usd == pytest.approx(0.5)


def test_load_reserve_cache_fee_unreadable_falls_back_to_gross(monkeypatch):
    from scanner import reserves
    asset = "0x" + "aa" * 20
    row = _fake_reserve(10500)
    row["underlyingAsset"] = asset
    base = (10**8, 10**8, 50_000_000, 8)
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache, _ = reserves.load_reserve_cache(_cache_fake_w3(row, base, fee_reverts=True))
    info = cache[asset]
    assert info["liquidationProtocolFee"] is None  # fee не прочитан — видимо
    assert info["liquidationBonus"] == pytest.approx(5.0)  # gross fallback
    assert info["liquidationBonusGross"] == pytest.approx(5.0)


def test_uidp_abi_matches_deployed_layout():
    # Регрессия #6: лишнее поле virtualAccActive (41 вместо 40) ломало decode
    # getReservesData целиком -> пустой кэш -> flat fallback. Layout сверен
    # с верифицированным деплоем 0x66E1...754f (Sourcify, chain 137).
    from scanner.reserves import UIDP_ABI
    fn = next(f for f in UIDP_ABI if f.get("name") == "getReservesData")
    comps = fn["outputs"][0]["components"]
    names = [c["name"] for c in comps]
    assert len(names) == 40
    assert "virtualAccActive" not in names
    # индексы, на которых держится _reserve_info (tuple-формат web3)
    assert names[0] == "underlyingAsset"
    assert names[2] == "symbol"
    assert names[3] == "decimals"
    assert names[6] == "reserveLiquidationBonus"
    assert names[9] == "borrowingEnabled"
    assert names[10] == "isActive"
    assert names[12] == "liquidityIndex"
    assert names[13] == "variableBorrowIndex"
    assert names[22] == "priceInMarketReferenceCurrency"


def test_load_reserve_cache_assigns_reserve_ids(monkeypatch):
    # id = индекс в getReservesData (порядок getReservesList) — нужен для eMode bitmap
    from scanner import reserves
    asset = "0x" + "aa" * 20
    row = _fake_reserve()
    row["underlyingAsset"] = asset
    base = (10**8, 10**8, 50_000_000, 8)
    cfg = (10500 << 32) | (1000 << 152)
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache, _ = reserves.load_reserve_cache(_cache_fake_w3(row, base, cfg=cfg))
    assert cache[asset]["id"] == 0


def _emode_fake_w3(bonus_raw=10100, bitmap=0b100, reverts=False):
    class FakeFunctions:
        def getEModeCategoryData(self, cid):
            if reverts:
                def boom():
                    raise ValueError("revert")
                return SimpleNamespace(call=boom)
            return SimpleNamespace(call=lambda: (9800, 9850, bonus_raw, "0x" + "00" * 20, "stable"))

        def getEModeCategoryCollateralBitmap(self, cid):
            return SimpleNamespace(call=lambda: bitmap)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    return FakeW3()


def test_emode_category_bonus_in_bitmap(monkeypatch):
    from scanner import reserves
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache = {}
    coll_info = {"id": 2, "liquidationProtocolFee": 1000}
    # бит 2 установлен -> коллатерал в категории -> gross 1% (10100)
    assert reserves.emode_category_bonus(_emode_fake_w3(bitmap=0b100), 1, coll_info, cache) == pytest.approx(1.0)
    # кэш: повторный вызов не ходит в RPC
    assert reserves.emode_category_bonus(None, 1, coll_info, cache) == pytest.approx(1.0)


def test_emode_category_bonus_not_in_bitmap(monkeypatch):
    from scanner import reserves
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache = {}
    coll_info = {"id": 2, "liquidationProtocolFee": 1000}
    # бит 2 НЕ установлен -> резервный бонус (None)
    assert reserves.emode_category_bonus(_emode_fake_w3(bitmap=0b010), 1, coll_info, cache) is None


def test_emode_category_bonus_unreadable_and_zero(monkeypatch):
    from scanner import reserves
    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    cache = {}
    coll_info = {"id": 2}
    assert reserves.emode_category_bonus(_emode_fake_w3(reverts=True), 1, coll_info, cache) is None
    assert cache[1] == (None, None)  # видимо как непрочитанное
    # категория 0 / нет id -> None без RPC
    assert reserves.emode_category_bonus(None, 0, coll_info, {}) is None
    assert reserves.emode_category_bonus(None, 1, {"id": None}, {}) is None
    assert reserves.emode_category_bonus(None, 1, None, {}) is None


def test_grace_period_until_reads_and_falls_back(monkeypatch):
    from scanner import reserves

    class FakeFunctions:
        def getLiquidationGracePeriod(self, asset):
            return SimpleNamespace(call=lambda: 1700000000)

    class FakeContract:
        functions = FakeFunctions()

    class FakeW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return FakeContract()

    monkeypatch.setattr(reserves, "rpc_call", lambda w3, fn, *a, **k: fn(*a, **k))
    assert reserves.grace_period_until(FakeW3(), "0x" + "aa" * 20) == 1700000000

    class BoomW3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                raise ValueError("revert")

    assert reserves.grace_period_until(BoomW3(), "0x" + "aa" * 20) == 0
