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
    assert pos == (a, 10, b, 50)  # долг — a, коллатерал — b (с флагом) + его scaled-баланс 50
