"""tests/test_calc.py
3 детерминированных теста на net_profit.
Из спеки: долг $200, бонус 5%, fee 0.05%, gas $0.01 → net $9.89.
"""
import pytest
from scanner.calc import net_profit

def test_spec_number():
    assert net_profit(200.0, 5.0, 0.05, 0.01) == pytest.approx(9.89)

def test_zero_when_bonus_equals_cost():
    # 100 * 1% - 100 * 0.9% - 0.10 = 1.00 - 0.90 - 0.10 = 0.0
    assert net_profit(100.0, 1.0, 0.9, 0.10) == pytest.approx(0.0)

def test_negative_when_bonus_below_cost():
    # 100 * 0.5% - 100 * 0.05% - 1.00 = 0.50 - 0.05 - 1.00 = -0.55
    assert net_profit(100.0, 0.5, 0.05, 1.00) == pytest.approx(-0.55)
