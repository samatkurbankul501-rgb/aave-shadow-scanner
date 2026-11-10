"""tests/test_parser.py — фикстурный тест продового парсера.

Из спеки: good → hf==1.12, collateral_usd==184.5, debt_usd==50.0, len==6;
bad → ValueError.

ВАЖНО: тесты импортируют scanner.parser.parse_account (продовый парсер),
а не локальный дубликат (как было до починки).
"""
import json
from pathlib import Path

import pytest

from scanner.parser import parse_account


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


def test_tuple_input():
    # Продовые getUserAccountData возвращают tuple(6) из web3, а не dict.
    data = (18_450_000_000, 5_000_000_000, 1_000_000_000, 800_000, 750_000_000_000_000_000, 1_120_000_000_000_000_000)
    parsed = parse_account(data)
    assert parsed["hf"] == pytest.approx(1.12)
    assert parsed["debt_usd"] == pytest.approx(50.0)
