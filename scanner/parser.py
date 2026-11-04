"""parser: парсинг ответа getUserAccountData Aave V3 Pool.

getUserAccountData возвращает 6 значений:
  totalCollateralBase, totalDebtBase, availableBorrowsBase,
  currentLiquidationThreshold, ltv, healthFactor.

totalCollateralBase / totalDebtBase / availableBorrowsBase — в Base Currency
с 8 децималями (BASE_CURRENCY_UNIT = 1e8). healthFactor — ray с 18 децималями.

Чистая функция без RPC. ValueError на отсутствующих/невалидных полях.
"""
BASE_CURRENCY_UNIT = 1e8  # 8 decimals, Aave V3 Base Currency
hf_scale = 1e18

_FIELDS = (
    "totalCollateralBase",
    "totalDebtBase",
    "availableBorrowsBase",
    "currentLiquidationThreshold",
    "ltv",
    "healthFactor",
)


def parse_account(data) -> dict:
    """Распарсить getUserAccountData (dict с именованными полями или
    tuple/list из 6 элементов от web3) в USD + HF.

    Returns: {collateral_usd, debt_usd, hf, available_borrows_usd, len}
    Raises: ValueError — если поля отсутствуют или имеют неверный тип.
    """
    vals = None
    if isinstance(data, dict):
        try:
            vals = [data[k] for k in _FIELDS]
        except (KeyError, TypeError) as e:
            raise ValueError(f"missing field: {e}") from e
    elif isinstance(data, (tuple, list)):
        if len(data) < 6:
            raise ValueError(f"expected 6 fields, got {len(data)}")
        vals = list(data[:6])
    else:
        raise ValueError(f"unsupported type: {type(data).__name__}")

    for k, v in zip(_FIELDS, vals):
        try:
            int(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"bad value for {k}: {v!r}") from e

    return {
        "collateral_usd": vals[0] / BASE_CURRENCY_UNIT,
        "debt_usd": vals[1] / BASE_CURRENCY_UNIT,
        "available_borrows_usd": vals[2] / BASE_CURRENCY_UNIT,
        "current_liquidation_threshold": vals[3],
        "ltv": vals[4],
        "hf": vals[5] / hf_scale,
        "len": 6,
    }
