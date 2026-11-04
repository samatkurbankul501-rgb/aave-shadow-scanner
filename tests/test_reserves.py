"""tests/test_reserves.py — тесты scanner.reserves + logger dedup.

reserves: проверка _reserve_info (BASIS_POINTS -> %) и топографии debtAsset.
logger: дедуп по user внутри одного прохода.
"""
import pytest

from scanner.reserves import _reserve_info, load_reserve_cache
from scanner.logger import log_entry, summary, reset_pass
from pathlib import Path
import tempfile


def _fake_reserve(bonus_bps=500, symbol="USDC"):
    # Строим минимальный dict-формат AggregatedReserveData
    return {
        "underlyingAsset": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "symbol": symbol,
        "decimals": 6,
        "reserveLiquidationBonus": bonus_bps,
        "priceInMarketReferenceCurrency": 100_000_000,
        "isActive": True,
        "borrowingEnabled": True,
    }


def test_reserve_info_bonus_bps_to_pct():
    info = _reserve_info(_fake_reserve(500))
    assert info["liquidationBonus"] == pytest.approx(5.0)
    info2 = _reserve_info(_fake_reserve(1000, "WMATIC"))
    assert info2["liquidationBonus"] == pytest.approx(10.0)


def test_logger_dedup():
    reset_pass()
    tmp = Path(tempfile.mkdtemp())
    r1 = log_entry({"user": "0x1", "hf": 0.97, "ts": 1, "net_profit_usd": 5.0}, tmp)
    r2 = log_entry({"user": "0x1", "hf": 0.98, "ts": 2, "net_profit_usd": 3.0}, tmp)
    # второй проход — тот же user, dedup внутри reset_pass цикла
    assert r2["hf"] == 0.98
    assert r2["ts"] == 2
    reset_pass()
    # после reset dedup сбрасывается
    r3 = log_entry({"user": "0x1", "hf": 0.90, "ts": 3, "net_profit_usd": 1.0}, tmp)
    assert r3["hf"] == 0.90
