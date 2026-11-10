"""calc: чистые функции без RPC.

Из спеки: net_profit_usd = (debt_usd * bonus_pct / 100) - (debt_usd * flash_fee_pct / 100) - gas_usd.
fee фикс 0.05% для оценки на этапе B; gas передаётся извне (оценка eth_estimateGas).
"""

RAY = 10 ** 27
BASE_CURRENCY_UNIT = 10 ** 8
CLOSE_FACTOR_HF_THRESHOLD = 0.95  # Aave V3: cap 50% только при HF СТРОГО выше
MIN_RESERVE_USD = 2000.0  # MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = 2000e8


def net_profit(debt_usd: float, bonus_pct: float, flash_fee_pct: float, gas_usd: float) -> float:
    """Чистый плюс ликвидации в USD.

    debt_usd: сумма долга (USD)
    bonus_pct: бонус протоковала (%)
    flash_fee_pct: комиссия flash loan (%)
    gas_usd: оценка газа (USD)
    Returns: net_profit_usd (может быть <= 0).
    """
    return debt_usd * bonus_pct / 100.0 - debt_usd * flash_fee_pct / 100.0 - gas_usd


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


def actual_debt_raw(scaled_debt: int, variable_borrow_index: int) -> int:
    """scaledVariableDebt -> реальный долг в raw-единицах токена (ray-математика, int)."""
    return scaled_debt * variable_borrow_index // RAY


def token_amount_usd(raw_amount: int, decimals: int, price_base: int) -> float:
    """raw-единицы токена -> USD. price_base — цена целого токена в base currency (1e8)."""
    return raw_amount / (10 ** decimals) * price_base / BASE_CURRENCY_UNIT
