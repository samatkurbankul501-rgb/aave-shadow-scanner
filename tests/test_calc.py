"""tests/test_calc.py
3 детерминированных теста на net_profit.
Из спеки: долг $200, бонус 5%, fee 0.05%, gas $0.01 → net $9.89.
"""
import pytest
from scanner.calc import RAY, actual_debt_raw, close_factor_pct, coverable_debt, executable_cover_usd, net_liquidation_bonus, net_profit, token_amount_usd

def test_spec_number():
    assert net_profit(200.0, 5.0, 0.05, 0.01) == pytest.approx(9.89)

def test_zero_when_bonus_equals_cost():
    # 100 * 1% - 100 * 0.9% - 0.10 = 1.00 - 0.90 - 0.10 = 0.0
    assert net_profit(100.0, 1.0, 0.9, 0.10) == pytest.approx(0.0)

def test_negative_when_bonus_below_cost():
    # 100 * 0.5% - 100 * 0.05% - 1.00 = 0.50 - 0.05 - 1.00 = -0.55
    assert net_profit(100.0, 0.5, 0.05, 1.00) == pytest.approx(-0.55)


def test_swap_zero_matches_legacy_formula():
    # swap_cost_pct=0 (дефолт) = обмен по паритету = старая формула и спека $9.89.
    assert net_profit(200.0, 5.0, 0.05, 0.01, 0.0) == pytest.approx(9.89)
    assert net_profit(200.0, 5.0, 0.05, 0.01, 0.0) == pytest.approx(net_profit(200.0, 5.0, 0.05, 0.01))


def test_swap_cost_exact_number():
    # collateral 100*1.05*0.997=104.685; flash repay 100*1.0005=100.05; net=4.635
    assert net_profit(100.0, 5.0, 0.05, 0.0, 0.3) == pytest.approx(4.635)


def test_swap_cost_reduces_profit():
    assert net_profit(100.0, 5.0, 0.05, 0.01, 0.3) < net_profit(100.0, 5.0, 0.05, 0.01, 0.0)


def test_swap_cost_can_eat_whole_bonus():
    # Бонус 5%, но swap 5% -> коллатерал 100*1.05*0.95=99.75 < flash repay 100.05 -> минус.
    assert net_profit(100.0, 5.0, 0.05, 0.0, 5.0) == pytest.approx(-0.30)
    assert net_profit(100.0, 5.0, 0.05, 0.0, 5.0) < 0


def test_net_liquidation_bonus_protocol_fee():
    # fee 1000 bps = 10% с бонуса: gross 5% -> net 4.5%; gross 7% -> net 6.3%.
    assert net_liquidation_bonus(5.0, 1000) == pytest.approx(4.5)
    assert net_liquidation_bonus(7.0, 1000) == pytest.approx(6.3)


def test_net_liquidation_bonus_zero_and_full_fee():
    assert net_liquidation_bonus(5.0, 0) == pytest.approx(5.0)  # fee 0 -> gross
    assert net_liquidation_bonus(5.0, 10000) == pytest.approx(0.0)  # 100% fee съедает бонус


def test_net_liquidation_bonus_unreadable_fee_is_gross():
    assert net_liquidation_bonus(5.0, None) == pytest.approx(5.0)


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


# executable_cover_usd: cap + достаточность коллатерала + анти-пылевое правило (#25)

def test_executable_cover_small_reserve_full():
    # Резерв < $2000 -> cap не применяется, весь долг покрываем
    assert executable_cover_usd(0.99, 1500.0, 5000.0, 1500.0, 5.0) == (1500.0, None)


def test_executable_cover_hf_below_threshold_full():
    # HF <= 0.95 -> cap нет; коллатерала $20000 хватает на долг $10000 + бонус 5%
    assert executable_cover_usd(0.95, 10000.0, 20000.0, 10000.0, 5.0) == (10000.0, None)


def test_executable_cover_cap_no_dust():
    # cap = 50% общего долга; остатки $5000/$4750 >= $1000 -> пыль не мешает
    assert executable_cover_usd(0.99, 10000.0, 10000.0, 10000.0, 5.0) == (5000.0, None)


def test_executable_cover_collateral_limited():
    # Коллатерала $1000 не хватает на долг $2000 с бонусом 5%:
    # контракт урежет долг до 1000/1.05 и изымет ВЕСЬ коллатерал (пыль не применяется)
    usd, limit = executable_cover_usd(0.99, 2000.0, 1000.0, 2000.0, 5.0)
    assert usd == pytest.approx(1000.0 / 1.05)
    assert limit == "collateral"


def test_executable_cover_dust_shrinks_debt_side():
    # HF 0.99, общий долг $5000 -> cap $2500; долг резерва $2600.
    # Наивный запрос $2500 оставил бы $100 долга < $1000 -> MustNotLeaveDust.
    # Исполнимо: $2600 - $1000 = $1600 (коллатерала хватает с запасом).
    usd, limit = executable_cover_usd(0.99, 2600.0, 10000.0, 5000.0, 5.0)
    assert usd == pytest.approx(1600.0)
    assert limit == "dust"


def test_executable_cover_dust_shrinks_collateral_side():
    # cap $2500, коллатерал $2700: остаток коллатерала должен быть >= $1000
    # -> покрываемо (2700-1000)/1.05 = $1619.05 (меньше, чем долг-сторона $2000)
    usd, limit = executable_cover_usd(0.99, 3000.0, 2700.0, 5000.0, 5.0)
    assert usd == pytest.approx(1700.0 / 1.05)
    assert limit == "dust"


def test_executable_cover_emode_gross_bonus():
    # eMode gross 1%: коллатерал-лимит = coll/1.01, пыль считается с gross
    usd, limit = executable_cover_usd(0.99, 2600.0, 10000.0, 5000.0, 1.0)
    assert usd == pytest.approx(1600.0)
    assert limit == "dust"
    usd2, limit2 = executable_cover_usd(0.99, 2000.0, 1000.0, 2000.0, 1.0)
    assert usd2 == pytest.approx(1000.0 / 1.01)
    assert limit2 == "collateral"
