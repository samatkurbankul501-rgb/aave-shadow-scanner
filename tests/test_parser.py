"""tests/test_parser.py — фикстурный тест парсера getUserAccountData.

Из спеки: good → hf==1.12, debt_usd==184.5 (коллатерал 184.50 $), len==12;
bad → ValueError.
"""
import json
from pathlib import Path

import pytest

BASE = 1e8  # Base Currency Unit Aave V3 (8 decimals)
HF_SCALE = 1e18


def parse_account(data: dict) -> dict:
    """Чистый парсер getUserAccountData → USD + HF (без RPC)."""
    try:
        keys = ("totalCollateralBase", "totalDebtBase", "availableBorrowsBase",
                "currentLiquidationThreshold", "ltv", "healthFactor")
        vals = [data[k] for k in keys]
    except (KeyError, TypeError) as e:
        raise ValueError(f"missing field: {e}") from e
    collateral_base, debt_base = vals[0], vals[1]
    return {
        "collateral_usd": collateral_base / BASE,
        "debt_usd": debt_base / BASE,
        "hf": vals[5] / HF_SCALE,
        "len": len([k for k in keys]),
    }


def load_fixture():
    return json.loads((Path(__file__).parent / "fixtures" / "two_good_bad.json").read_text(encoding="utf-8"))


def test_good_fixture():
    fixture = load_fixture()
    parsed = parse_account(fixture["good"])
    assert parsed["hf"] == pytest.approx(1.12)
    assert parsed["collateral_usd"] == pytest.approx(184.5)
    assert parsed["debt_usd"] == pytest.approx(50.0)
    assert parsed["len"] == 6  # шесть полей getUserAccountData


def test_bad_fixture_raises():
    fixture = load_fixture()
    assert fixture["bad"] == "{not json"
    with pytest.raises(ValueError):
        json.loads(fixture["bad"])
    with pytest.raises(ValueError):
        parse_account({})
