"""reserves: кэш getReservesData + getUserReservesData (Aave V3).

Решающие реальности:
- бонус ликвидации и closeFactor — per-reserve, а не плоские 5%/50%;
- debtAsset нельзя угадывать из getUserAccountData (там только агрегат в USD);
  реальный долг виден через UiPoolDataProvider.getUserReservesData(provider, user)
  (подтягивает scaledVariableDebt + scaledATokenBalance по резервам),
  либо AaveProtocolDataProvider.getUserReserveData(asset, user).

После кэширования резерва мы: (1) не дёргаем getReservesData на каждого юзера,
(2) берём per-reserve liquidationBonus из AggregatedReserveData
reserveLiquidationBonus; closeFactor считается по контракту в calc.coverable_debt
(100% по умолчанию; cap 50% только при HF > 0.95 и обоих резервах >= $2000),
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

def load_reserve_cache(w3, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON) -> tuple[dict, float | None]:
    """Кэшировать getReservesData; ключ underlyingAsset -> info.

    Returns: (cache, pol_usd) — цена сетевого токена (POL) в USD из оракула
    Aave (base tuple networkBaseTokenPriceInUsd), None если недоступна.
    Если UiPoolDataProvider недоступен/ABI не совпал (RPC down / revert /
    decode error), возвращается ({}, None) — вызов берёт flat допущения;
    пустой кэш обязан быть виден в сводном JSON (reserve_cache_size=0).
    """
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        reserves, base = rpc_call(w3, c.functions.getReservesData(provider).call)
    except Exception as e:
        print(f"reserves cache miss: getReservesData failed: {e}")
        return {}, None
    out = {}
    for r in reserves:
        asset = r["underlyingAsset"] if isinstance(r, dict) else r[0]
        out[asset] = _reserve_info(r)
    pol_usd = None
    try:
        price, decimals = base[2], base[3]
        if price > 0:
            pol_usd = price / (10 ** decimals)
    except Exception:
        pass
    return out, pol_usd


def _reserve_info(r) -> dict:
    """Извлечь liquidationBonus (%), decimals, symbol из AggregatedReserveData."""
    def get(key, idx):
        return r[key] if isinstance(r, dict) else r[idx]
    return {
        "underlyingAsset": get("underlyingAsset", 0),
        "symbol": get("symbol", 2),
        "decimals": get("decimals", 3),
        "liquidationBonus": (float(get("reserveLiquidationBonus", 6)) - 10000.0) / 100.0,  # множитель bps (10500 = +5%) -> бонус %
        "variableBorrowIndex": get("variableBorrowIndex", 13),  # ray: scaledDebt * index // 1e27 = реальный долг
        "liquidityIndex": get("liquidityIndex", 12),  # ray: scaledATokenBalance * index // 1e27 = реальный коллатерал
        "price_in_market_ccy": get("priceInMarketReferenceCurrency", 22),
        "isActive": bool(get("isActive", 10)),
        "borrowingEnabled": bool(get("borrowingEnabled", 9)),
    }


def top_position(w3, user: str, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON, block_identifier=None) -> tuple | None:
    """Реальные debtAsset (max scaledVariableDebt) и collateralAsset (max scaledATokenBalance).

    Returns: (debt_asset, scaled_debt, collateral_asset | None, coll_scaled_atoken)
    или None при пустой позиции / ошибке. coll_scaled_atoken — scaled-баланс
    aToken коллатерала (0 если нет): нужен для closeFactor по контракту
    (коллатерал резерва в USD >= $2000). collateral_asset — placeholder для
    estimate_gas: liquidationCall требует адрес коллатерала, но для оценки газа
    точное совпадение неважно; раньше подстановка POOL давала revert 100%.
    block_identifier пиннит чтение к блоку лога (иначе штамп блока в JSONL врёт).
    """
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        user_res, _ = rpc_call(w3, c.functions.getUserReservesData(provider, user).call, block_identifier=block_identifier)
    except Exception:
        return None
    best_debt = None
    best_coll = None
    for u in user_res:
        if isinstance(u, dict):
            a, a_bal, coll_enabled, scaled = u["underlyingAsset"], u["scaledATokenBalance"], u["usageAsCollateralEnabledOnUser"], u["scaledVariableDebt"]
        else:
            a, a_bal, coll_enabled, scaled = u[0], u[1], u[2], u[3]
        if scaled > 0 and (best_debt is None or scaled > best_debt[1]):
            best_debt = (a, scaled)
        # Коллатералом считаем только актив с включённым флагом у юзера:
        # иначе estimateGas получал не-коллатерал и ревертил.
        if coll_enabled and a_bal > 0 and (best_coll is None or a_bal > best_coll[1]):
            best_coll = (a, a_bal)
    if best_debt is None:
        return None
    return best_debt[0], best_debt[1], (best_coll[0] if best_coll else None), (best_coll[1] if best_coll else 0)
