"""reserves: кэш getReservesData + getUserReservesData (Aave V3).

Решающие реальности:
- бонус ликвидации и closeFactor — per-reserve, а не плоские 5%/50%;
- debtAsset нельзя угадывать из getUserAccountData (там только агрегат в USD);
  реальный долг виден через UiPoolDataProvider.getUserReservesData(provider, user)
  (подтягивает scaledVariableDebt + scaledATokenBalance по резервам),
  либо AaveProtocolDataProvider.getUserReserveData(asset, user).

После кэширования резерва мы: (1) не дёргаем getReservesData на каждого юзера,
(2) берём per-reserve liquidationBonus из AggregatedReserveData
reserveLiquidationBonus и closeFactor неявно (в Aave V3 closeFactor фиксирован
на уровне протокола: max 50%, и в большинстве случаев равен 50% — берём 50%
как тот же допуск, но с per-reserve бонусом вместо плоского),
(3) различаем collateralAsset/debtAsset для estimate_gas.
"""
from scanner.rpc_client import rpc_call

PROVIDER_POLYGON = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
DATA_PROVIDER_POLYGON = "0x66E1aBdb06e7363a618D65a910c540dfED23754f"

UIDP_ABI = [
    {
        "inputs": [{"name": "provider", "type": "address"}],
        "name": "getReservesList",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "provider", "type": "address"}],
        "name": "getReservesData",
        "outputs": [
            {
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "name", "type": "string"},
                    {"name": "symbol", "type": "string"},
                    {"name": "decimals", "type": "uint256"},
                    {"name": "baseLTVasCollateral", "type": "uint256"},
                    {"name": "reserveLiquidationThreshold", "type": "uint256"},
                    {"name": "reserveLiquidationBonus", "type": "uint256"},
                    {"name": "reserveFactor", "type": "uint256"},
                    {"name": "usageAsCollateralEnabled", "type": "bool"},
                    {"name": "borrowingEnabled", "type": "bool"},
                    {"name": "isActive", "type": "bool"},
                    {"name": "isFrozen", "type": "bool"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "liquidityRate", "type": "uint128"},
                    {"name": "variableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "availableLiquidity", "type": "uint256"},
                    {"name": "totalScaledVariableDebt", "type": "uint256"},
                    {"name": "priceInMarketReferenceCurrency", "type": "uint256"},
                    {"name": "priceOracle", "type": "address"},
                    {"name": "variableRateSlope1", "type": "uint256"},
                    {"name": "variableRateSlope2", "type": "uint256"},
                    {"name": "baseVariableBorrowRate", "type": "uint256"},
                    {"name": "optimalUsageRatio", "type": "uint256"},
                    {"name": "isPaused", "type": "bool"},
                    {"name": "isSiloedBorrowing", "type": "bool"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                    {"name": "flashLoanEnabled", "type": "bool"},
                    {"name": "debtCeiling", "type": "uint256"},
                    {"name": "debtCeilingDecimals", "type": "uint256"},
                    {"name": "borrowCap", "type": "uint256"},
                    {"name": "supplyCap", "type": "uint256"},
                    {"name": "borrowableInIsolation", "type": "bool"},
                    {"name": "virtualAccActive", "type": "bool"},
                    {"name": "virtualUnderlyingBalance", "type": "uint128"},
                    {"name": "deficit", "type": "uint128"},
                ],
                "name": "",
                "type": "tuple[]",
            },
            {
                "components": [
                    {"name": "marketReferenceCurrencyUnit", "type": "uint256"},
                    {"name": "marketReferenceCurrencyPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceDecimals", "type": "uint8"},
                ],
                "name": "",
                "type": "tuple",
            },
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "name": "getUserReservesData",
        "outputs": [
            {
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "scaledATokenBalance", "type": "uint256"},
                    {"name": "usageAsCollateralEnabledOnUser", "type": "bool"},
                    {"name": "scaledVariableDebt", "type": "uint256"},
                ],
                "name": "",
                "type": "tuple[]",
            },
            {"name": "", "type": "uint8"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
];

_cache = {}
_user_cache = {}


def load_reserve_cache(w3, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON) -> dict:
    """Кэшировать getReservesData; ключ underlyingAsset -> info.

    Если UiPoolDataProvider недоступен (RPC down / revert), возвращается пустой
    dict (нет кэша -> вызовер берёт flat допущения).
    """
    global _cache
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        reserves, _base = rpc_call(w3, c.functions.getReservesData(provider).call)
    except Exception as e:
        print(f"reserves cache miss: getReservesData failed: {e}")
        return {}
    out = {}
    for r in reserves:
        asset = r["underlyingAsset"] if isinstance(r, dict) else r[0]
        out[asset] = _reserve_info(r)
    _cache = out
    return out


def _reserve_info(r) -> dict:
    """Извлечь liquidationBonus (%), decimals, symbol из AggregatedReserveData."""
    def get(key, idx):
        return r[key] if isinstance(r, dict) else r[idx]
    return {
        "underlyingAsset": get("underlyingAsset", 0),
        "symbol": get("symbol", 2),
        "decimals": get("decimals", 3),
        "liquidationBonus": float(get("reserveLiquidationBonus", 6)) / 100.0,  # BASIS_POINTS -> %
        "price_in_market_ccy": get("priceInMarketReferenceCurrency", 22),
        "isActive": bool(get("isActive", 10)),
        "borrowingEnabled": bool(get("borrowingEnabled", 9)),
    }


def top_debt_asset(w3, user: str, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON) -> tuple | None:
    """Реальный debtAsset с max scaledVariableDebt для user.

    Returns: (asset_address, scaledDebt) или None при пустой позиции / ошибке.
    Используется в estimate_gas вместо подстановки POOL как заглушки.
    """
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        user_res, _ = rpc_call(w3, c.functions.getUserReservesData(provider, user).call)
    except Exception:
        return None
    best = None
    for u in user_res:
        a, scaled = (u["underlyingAsset"], u["scaledVariableDebt"]) if isinstance(u, dict) else (u[0], u[3])
        if scaled == 0:
            continue
        if best is None or scaled > best[1]:
            best = (a, scaled)
    return best
