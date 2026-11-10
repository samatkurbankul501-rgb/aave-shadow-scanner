"""tests/test_reserves.py — тесты scanner.reserves + logger dedup.

reserves: проверка _reserve_info (множитель bps -> %) и топографии debtAsset.
logger: дедуп по user внутри одного прохода.
"""
import json

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
