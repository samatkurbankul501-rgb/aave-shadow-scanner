"""calc: чистые функции без RPC.

Из спеки: net_profit_usd = debt_usd*(1+bonus_pct/100)*(1-swap_cost_pct/100)
                          - debt_usd*(1+flash_fee_pct/100) - gas_usd.
fee фикс 0.05% для оценки на этапе B; gas передаётся извне (оценка eth_estimateGas);
swap_cost_pct — издержки обмена изъятого коллатерала обратно в debt-актив
(комиссия DEX + slippage). На этапе B это допущение; swap_cost_pct=0 даёт
старую формулу (обмен по паритету) и спечную цифру $9.89.
"""

RAY = 10 ** 27
BASE_CURRENCY_UNIT = 10 ** 8
CLOSE_FACTOR_HF_THRESHOLD = 0.95  # Aave V3: cap 50% только при HF СТРОГО выше
MIN_RESERVE_USD = 2000.0  # MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = 2000e8
MIN_LEFTOVER_BASE_USD = 1000.0  # MIN_LEFTOVER_BASE = 2000e8/2 (origin LiquidationLogic:64)
LIQUIDATION_PROTOCOL_FEE_UNIT = 10000  # fee в bps от бонусной части: 10000 = 100% бонуса


def net_profit(debt_usd: float, bonus_pct: float, flash_fee_pct: float, gas_usd: float, swap_cost_pct: float = 0.0) -> float:
    """Чистый плюс ликвидации в USD.

    Модель: flash-взаймы debt_usd, гасим долг, изымаем коллатерал на
    debt_usd*(1+bonus_pct/100), меняем его обратно в debt-актив с издержками
    swap_cost_pct%, возвращаем flash-тело с комиссией debt_usd*(1+flash_fee_pct/100).

    debt_usd: покрываемый долг (USD)
    bonus_pct: бонус ликвидации (%)
    flash_fee_pct: комиссия flash loan (%)
    gas_usd: оценка газа (USD)
    swap_cost_pct: издержки swap коллатерала (%) — комиссия DEX + slippage;
        0.0 = обмен по паритету (старая формула, спека $9.89).
    Returns: net_profit_usd (может быть <= 0).
    """
    collateral_after_swap = debt_usd * (1.0 + bonus_pct / 100.0) * (1.0 - swap_cost_pct / 100.0)
    flash_repay = debt_usd * (1.0 + flash_fee_pct / 100.0)
    return collateral_after_swap - flash_repay - gas_usd


def net_liquidation_bonus(gross_bonus_pct: float, protocol_fee_bps: int) -> float:
    """Net-бонус ликвидатора после среза protocol fee с бонусной части коллатерала.

    Контракт (LiquidationLogic:614-623): fee = bonusCollateral × fee_bps/10000,
    где bonusCollateral — сама бонусная надбавка. Поэтому net = gross × (1 − fee_bps/10000).
    Пример: gross +5%, fee 1000 bps (10% с бонуса) -> net +4.5%.
    protocol_fee_bps=None (fee не прочитан) -> gross (допущение, видимо в логе).
    """
    if protocol_fee_bps is None:
        return gross_bonus_pct
    return gross_bonus_pct * (1.0 - protocol_fee_bps / LIQUIDATION_PROTOCOL_FEE_UNIT)


def close_factor_pct(hf: float) -> float:
    """Fallback без данных резервов: HF <= 0.95 -> 100%, иначе 50%.

    Точное правило — coverable_debt (контракт требует ещё >= $2000 по резерву).
    Граница: LiquidationLogic использует `healthFactor > 0.95e18` для cap,
    поэтому ровно 0.95 — это 100%.
    """
    return 100.0 if hf <= CLOSE_FACTOR_HF_THRESHOLD else 50.0


def coverable_debt(hf: float, reserve_debt_usd: float, reserve_coll_usd: float, total_debt_usd: float) -> float:
    """Зеркало Aave V3 LiquidationLogic: какая ДОЛЯ долга топ-резерва покрываема.

    По умолчанию 100% долга резерва. Cap применяется только когда
    долг резерва >= $2000 И коллатерал резерва >= $2000 И HF > 0.95;
    cap = 50% ОБЩЕГО долга: если долг резерва в него укладывается — 100%,
    иначе покрываема доля cap / reserve_debt (контракт: maxLiquidatableDebt =
    totalDebt.percentMul(50%), переведённый в raw debt-актива по цене).
    """
    if (reserve_debt_usd >= MIN_RESERVE_USD
            and reserve_coll_usd >= MIN_RESERVE_USD
            and hf > CLOSE_FACTOR_HF_THRESHOLD):
        cap_usd = total_debt_usd * 0.5
        if reserve_debt_usd > cap_usd:
            return cap_usd / reserve_debt_usd
    return 1.0


def executable_cover_usd(hf: float, reserve_debt_usd: float, reserve_coll_usd: float,
                         total_debt_usd: float, gross_bonus_pct: float) -> tuple[float, str | None]:
    """Сколько долга (USD) РЕАЛЬНО покрываемо ликвидацией и что ограничивает.

    Зеркало развёрнутого origin LiquidationLogic:
    - closeFactor cap (coverable_debt);
    - нехватка коллатерала (605-612): контракт урезает долг до
      collateral/(1+gross) и изымает ВЕСЬ коллатерал — пылевое правило не применяется;
    - анти-пылевое правило MIN_LEFTOVER_BASE (299-325): частичная ликвидация
      обязана оставить >= $1000 и долга, и коллатерала в резерве, иначе revert
      MustNotLeaveDust; запрос урезается до исполнимого.
    gross_bonus_pct — GROSS-бонус (коллатерал изымается с gross, fee срезается после).
    Returns (cover_usd, limit): limit None | "collateral" | "dust".
    """
    frac = coverable_debt(hf, reserve_debt_usd, reserve_coll_usd, total_debt_usd)
    capped = reserve_debt_usd * frac
    gross_mult = 1.0 + gross_bonus_pct / 100.0
    coll_limited = reserve_coll_usd / gross_mult
    if coll_limited < capped:
        return coll_limited, "collateral"
    if capped >= reserve_debt_usd:
        return reserve_debt_usd, None
    eff = min(capped,
              reserve_debt_usd - MIN_LEFTOVER_BASE_USD,
              (reserve_coll_usd - MIN_LEFTOVER_BASE_USD) / gross_mult)
    if eff < capped:
        return max(eff, 0.0), "dust"
    return capped, None


def actual_debt_raw(scaled_debt: int, variable_borrow_index: int) -> int:
    """scaledVariableDebt -> реальный долг в raw-единицах токена (ray-математика, int)."""
    return scaled_debt * variable_borrow_index // RAY


def token_amount_usd(raw_amount: int, decimals: int, price_base: int) -> float:
    """raw-единицы токена -> USD. price_base — цена целого токена в base currency (1e8)."""
    return raw_amount / (10 ** decimals) * price_base / BASE_CURRENCY_UNIT
