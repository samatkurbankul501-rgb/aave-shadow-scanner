"""tests/test_calc.py
3 детерминированных теста на net_profit.
Из спеки: долг $200, бонус 5%, fee 0.05%, gas $0.01 → net $9.89.
"""
import pytest
from scanner.calc import RAY, actual_debt_raw, close_factor_pct, coverable_debt, net_profit, token_amount_usd

def test_spec_number():
    assert net_profit(200.0, 5.0, 0.05, 0.01) == pytest.approx(9.89)

def test_zero_when_bonus_equals_cost():
    # 100 * 1% - 100 * 0.9% - 0.10 = 1.00 - 0.90 - 0.10 = 0.0
    assert net_profit(100.0, 1.0, 0.9, 0.10) == pytest.approx(0.0)

def test_negative_when_bonus_below_cost():
    # 100 * 0.5% - 100 * 0.05% - 1.00 = 0.50 - 0.05 - 1.00 = -0.55
    assert net_profit(100.0, 0.5, 0.05, 1.00) == pytest.approx(-0.55)


def test_close_factor_full_below_095():
    # Aave V3 LiquidationLogic: cap только при HF СТРОГО > 0.95.
    # Ровно 0.95 -> 100% (контракт использует `healthFactor > CLOSE_FACTOR_HF_THRESHOLD`).
    assert close_factor_pct(0.94) == 100.0
    assert close_factor_pct(0.95) == 100.0
    assert close_factor_pct(0.999) == 50.0


def test_coverable_debt_small_reserve_always_full():
    # Долг или коллатерал резерва < $2000 -> 100% независимо от HF.
    assert coverable_debt(0.99, 1500.0, 5000.0, 1500.0) == 1.0  # долг мал
    assert coverable_debt(0.99, 5000.0, 1500.0, 5000.0) == 1.0  # коллатерал мал
    assert coverable_debt(0.99, 1999.0, 1999.0, 1999.0) == 1.0


def test_coverable_debt_hf_at_or_below_threshold_full():
    # HF <= 0.95 -> 100% даже на больших резервах.
    assert coverable_debt(0.95, 10000.0, 10000.0, 10000.0) == 1.0
    assert coverable_debt(0.90, 10000.0, 10000.0, 10000.0) == 1.0


def test_coverable_debt_cap_is_half_of_total_not_reserve():
    # Большой резерв, HF > 0.95: cap = 50% ОБЩЕГО долга.
    # Долг резерва == общий долг -> покрываема ровно половина резерва.
    assert coverable_debt(0.99, 10000.0, 10000.0, 10000.0) == pytest.approx(0.5)
    # Резерв — лишь часть общего долга и укладывается в cap -> 100% резерва.
    assert coverable_debt(0.99, 3000.0, 10000.0, 10000.0) == 1.0  # 3000 <= 5000 cap
    # Резерв больше cap, но не весь долг: доля = cap / reserve_debt.
    assert coverable_debt(0.99, 8000.0, 10000.0, 10000.0) == pytest.approx(5000.0 / 8000.0)


def test_actual_debt_raw_ray_integer_precision():
    assert actual_debt_raw(10**20, 2 * RAY) == 2 * 10**20
    big = 2**60 + 1  # float-мантисса потеряла бы единицу
    assert actual_debt_raw(big, RAY) == big


def test_token_amount_usd_decimals_and_price():
    assert token_amount_usd(100 * 10**6, 6, 10**8) == pytest.approx(100.0)  # 100 USDC по $1
    assert token_amount_usd(10**18, 18, 2000 * 10**8) == pytest.approx(2000.0)  # 1 WETH по $2000
