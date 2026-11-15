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
from scanner.discovery import POOL_POLYGON
from scanner.calc import RAY, net_liquidation_bonus, token_amount_usd

PROVIDER_POLYGON = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
DATA_PROVIDER_POLYGON = "0x66E1aBdb06e7363a618D65a910c540dfED23754f"

# ABI сверен с верифицированным деплоем (Sourcify, chain 137): 40 полей
# AggregatedReserveData; лишнее поле (virtualAccActive) ломает decode целиком.
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

# liquidationProtocolFee — биты 152-167 конфигурации резерва (ReserveConfiguration.sol:
# LIQUIDATION_PROTOCOL_FEE_START_BIT_POSITION=152, маска 0xFFFF). Fee в bps от бонусной
# части: 1000 = 10% с бонуса. И бонус, и fee — свойства КОЛЛАТЕРАЛЬНОГО резерва
# (LiquidationLogic:233-238, 595), не debt-резерва.
LIQ_PROTOCOL_FEE_START_BIT = 152
LIQ_PROTOCOL_FEE_MASK = 0xFFFF

POOL_CONFIG_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getConfiguration",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
];

# eMode: развёрнутый Pool.getEModeCategoryData возвращает EModeCategoryLegacy
# (ltv, liquidationThreshold, liquidationBonus, priceSource, label) — бонус idx 2;
# членство коллатерала в категории — по collateralBitmap (бит = reserve id).
# Сверено с развёрнутым impl 0x9a37...41aa (Sourcify full match, aave-v3-origin).
EMODE_ABI = [
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryData",
     "outputs": [{"components": [
         {"name": "ltv", "type": "uint16"},
         {"name": "liquidationThreshold", "type": "uint16"},
         {"name": "liquidationBonus", "type": "uint16"},
         {"name": "priceSource", "type": "address"},
         {"name": "label", "type": "string"}], "name": "", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryCollateralBitmap",
     "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
];

POOL_GRACE_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getLiquidationGracePeriod",
     "outputs": [{"name": "", "type": "uint40"}], "stateMutability": "view", "type": "function"},
];


def load_reserve_cache(w3, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON, pool: str = POOL_POLYGON) -> tuple[dict, float | None]:
    """Кэшировать getReservesData; ключ underlyingAsset -> info.

    Returns: (cache, pol_usd) — цена сетевого токена (POL) в USD из оракула
    Aave (base tuple networkBaseTokenPriceInUsd), None если недоступна.
    Если UiPoolDataProvider недоступен/ABI не совпал (RPC down / revert /
    decode error), возвращается ({}, None) — вызов берёт flat допущения;
    пустой кэш обязан быть виден в сводном JSON (reserve_cache_size=0).

    info["liquidationBonus"] — NET-бонус после protocol fee (для модели прибыли);
    info["liquidationBonusGross"] — gross из reserveLiquidationBonus;
    info["liquidationProtocolFee"] — fee в bps с бонуса (None если не прочитан).
    """
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        reserves, base = rpc_call(w3, c.functions.getReservesData(provider).call)
    except Exception as e:
        print(f"reserves cache miss: getReservesData failed: {e}")
        return {}, None
    out = {}
    for i, r in enumerate(reserves):
        asset = r["underlyingAsset"] if isinstance(r, dict) else r[0]
        info = _reserve_info(r)
        # id = индекс в getReservesList (UIDP идёт по списку по порядку);
        # нужен для проверки членства коллатерала в eMode collateralBitmap
        info["id"] = i
        out[asset] = info
    # Protocol fee с бонуса + net-бонус: Pool.getConfiguration per-reserve.
    # Fee не прочитан -> gross (видимо: liquidationProtocolFee=None + счётчик в summary).
    pool_c = w3.eth.contract(address=pool, abi=POOL_CONFIG_ABI)
    for asset, info in out.items():
        gross = info["liquidationBonus"]
        info["liquidationBonusGross"] = gross
        try:
            cfg = rpc_call(w3, pool_c.functions.getConfiguration(asset).call)
            fee = (cfg >> LIQ_PROTOCOL_FEE_START_BIT) & LIQ_PROTOCOL_FEE_MASK
        except SystemExit:
            raise
        except Exception:
            fee = None
        info["liquidationProtocolFee"] = fee
        info["liquidationBonus"] = net_liquidation_bonus(gross, fee)
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
        "isPaused": bool(get("isPaused", 28)),
        "borrowingEnabled": bool(get("borrowingEnabled", 9)),
    }


def _usd_value(scaled: int, index_key: str, info: dict | None) -> float | None:
    """scaled-баланс -> USD через index/decimals/цену резерва; None если резерв не в кэше."""
    if not info or info["price_in_market_ccy"] <= 0:
        return None
    raw = scaled * info[index_key] // RAY
    return token_amount_usd(raw, info["decimals"], info["price_in_market_ccy"])


def top_position(w3, user: str, provider: str = PROVIDER_POLYGON, uidp: str = DATA_PROVIDER_POLYGON,
                 reserve_cache: dict | None = None, block_identifier=None) -> tuple | None:
    """Реальные debtAsset (max USD долга) и collateralAsset (max USD коллатерала).

    Returns: (debt_asset, scaled_debt, collateral_asset | None, coll_scaled_atoken,
    emode_category) или None при пустой позиции / ошибке.
    Ранжирование по USD через reserve_cache (#24: scaled-балансы разных активов
    несравнимы — decimals/цена разные); без кэша — legacy-ранжирование по scaled.
    coll_scaled_atoken — scaled-баланс aToken коллатерала (0 если нет): нужен для
    closeFactor по контракту (коллатерал резерва в USD >= $2000).
    emode_category — категория юзера (второй возврат getUserReservesData): при
    eMode != 0 и коллатерале в bitmap категории бонус берётся из категории
    (LiquidationLogic:226-239), а не из резерва.
    block_identifier пиннит чтение к блоку лога (иначе штамп блока в JSONL врёт).
    """
    c = w3.eth.contract(address=uidp, abi=UIDP_ABI)
    try:
        user_res, emode = rpc_call(w3, c.functions.getUserReservesData(provider, user).call, block_identifier=block_identifier)
    except Exception:
        return None
    cache = reserve_cache or {}
    best_debt = None  # (asset, scaled, rank_key)
    best_coll = None
    for u in user_res:
        if isinstance(u, dict):
            a, a_bal, coll_enabled, scaled = u["underlyingAsset"], u["scaledATokenBalance"], u["usageAsCollateralEnabledOnUser"], u["scaledVariableDebt"]
        else:
            a, a_bal, coll_enabled, scaled = u[0], u[1], u[2], u[3]
        if scaled > 0:
            usd = _usd_value(scaled, "variableBorrowIndex", cache.get(a))
            key = (usd if usd is not None else -1.0, scaled)
            if best_debt is None or key > best_debt[2]:
                best_debt = (a, scaled, key)
        # Коллатералом считаем только актив с включённым флагом у юзера:
        # иначе estimateGas получал не-коллатерал и ревертил.
        if coll_enabled and a_bal > 0:
            usd = _usd_value(a_bal, "liquidityIndex", cache.get(a))
            key = (usd if usd is not None else -1.0, a_bal)
            if best_coll is None or key > best_coll[2]:
                best_coll = (a, a_bal, key)
    if best_debt is None:
        return None
    return best_debt[0], best_debt[1], (best_coll[0] if best_coll else None), (best_coll[1] if best_coll else 0), int(emode)


def emode_category_bonus(w3, category: int, coll_info: dict | None, cache: dict,
                         pool: str = POOL_POLYGON) -> float | None:
    """GROSS-бонус (%) eMode-категории, если коллатерал в её collateralBitmap; иначе None.

    Развёрнутый LiquidationLogic:226-239: при borrowerEModeCategory != 0 и
    коллатерале в bitmap категории бонус = категория, не резерв. Protocol fee
    при этом остаётся от коллатерального резерва (595-623) — net считает вызывающий.
    cache: category -> (gross_pct | None, bitmap | None) на проход;
    (None, None) = данные не прочитаны (вызывающий помечает fallback).
    """
    if not category or coll_info is None or coll_info.get("id") is None:
        return None
    if category not in cache:
        try:
            c = w3.eth.contract(address=pool, abi=EMODE_ABI)
            data = rpc_call(w3, c.functions.getEModeCategoryData(category).call)
            bitmap = rpc_call(w3, c.functions.getEModeCategoryCollateralBitmap(category).call)
            raw_bonus = data["liquidationBonus"] if isinstance(data, dict) else data[2]
            cache[category] = ((float(raw_bonus) - 10000.0) / 100.0, int(bitmap))
        except SystemExit:
            raise
        except Exception:
            cache[category] = (None, None)
    gross, bitmap = cache[category]
    if gross is None or bitmap is None:
        return None
    if bitmap & (1 << coll_info["id"]):
        return gross
    return None


def grace_period_until(w3, asset: str, pool: str = POOL_POLYGON) -> int:
    """unix-время окончания grace-периода ликвидации резерва (0 = нет/не прочитан).

    Развёрнутый ValidationLogic требует liquidationGracePeriodUntil < block.timestamp
    для обоих резервов; непрочитанный период считаем истёкшим (estimateGas словит revert).
    """
    try:
        c = w3.eth.contract(address=pool, abi=POOL_GRACE_ABI)
        return int(rpc_call(w3, c.functions.getLiquidationGracePeriod(asset).call))
    except SystemExit:
        raise
    except Exception:
        return 0
