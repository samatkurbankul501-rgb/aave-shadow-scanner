"""calc: чистая функция net_profit без RPC.

Из спеки: net_profit_usd = (debt_usd * bonus_pct / 100) - (debt_usd * flash_fee_pct / 100) - gas_usd.
fee фикс 0.05% для оценки на этапе B; gas передаётся извне (оценка eth_estimateGas).
"""

def net_profit(debt_usd: float, bonus_pct: float, flash_fee_pct: float, gas_usd: float) -> float:
    """Чистый плюс ликвидации в USD.

    debt_usd: сумма долга (USD)
    bonus_pct: бонус протоковала (%)
    flash_fee_pct: комиссия flash loan (%)
    gas_usd: оценка газа (USD)
    Returns: net_profit_usd (может быть <= 0).
    """
    return debt_usd * bonus_pct / 100.0 - debt_usd * flash_fee_pct / 100.0 - gas_usd
