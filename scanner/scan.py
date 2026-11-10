"""scan.py: точка входа shadow-скана (этап B, только чтение, без ключей).

Поток: connect -> discovery (курсор до safe_block) -> getUserAccountData по
вотч-листу -> фильтр HF<1.0 + долг>=$50 -> оценка net_profit -> logger.

Честные допущения этапа B (помечены в логе как допущения, не факты):
- FLASH_FEE_PCT=0.05 — допущение flash fee (spec: 0.05% фикс для оценки);
- gas_usd: живой gas_price через w3.eth.gas_price; цена POL — из оракула
  Aave (getReservesData), fallback-допущение POL_USD_ASSUMED; gas_price
  fallback — GAS_GWEI_ASSUMED.

Реализм оценки (исправления 2026-11-10):
- все чтения пользователя (getUserAccountData, getUserReservesData,
  estimateGas) пиннятся к last_ok: block/block_hash в JSONL — правда;
- closeFactor по контракту (LiquidationLogic): 100% долга резерва по умолчанию;
  cap 50% ОБЩЕГО долга только при HF > 0.95 и обоих резервах >= $2000;
  без данных коллатерала — fallback close_factor_pct(hf);
- debtToCover считается от топ-одного debt-актива: scaledDebt *
  variableBorrowIndex // RAY (целочисленно), USD через per-reserve цену;
  агрегат getUserAccountData — только fallback без данных позиции/кэша;
- estimateGas от реального ликвидатора из свежих LiquidationCall-логов
  (проверены balanceOf + allowance debtAsset к Pool). ZERO_ADDRESS давал
  гарантированный revert. Нет подходящего ликвидатора / revert — флаг
  gas_assumed и fallback-газ GAS_EST_DEFAULT; доля fallbackа в сводном JSON.
"""
import argparse
import time
from pathlib import Path

from web3 import Web3

from scanner.calc import actual_debt_raw, close_factor_pct, coverable_debt, net_profit, token_amount_usd
from scanner.discovery import POOL_POLYGON, load_cursor, save_cursor, scan_range
from scanner.logger import log_entry, reset_pass, summary
from scanner.parser import parse_account
from scanner.reserves import load_reserve_cache, top_position
from scanner.rpc_client import connect, get_block_number, get_rl_hits, rpc_call

POOL_ABI = [
    {"inputs": [{"internalType": "address", "name": "user", "type": "address"}], "name": "getUserAccountData", "outputs": [{"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"}, {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"}, {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"}, {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"}, {"internalType": "uint256", "name": "ltv", "type": "uint256"}, {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "collateralAsset", "type": "address"}, {"internalType": "address", "name": "debtAsset", "type": "address"}, {"internalType": "address", "name": "user", "type": "address"}, {"internalType": "uint256", "name": "debtToCover", "type": "uint256"}, {"internalType": "bool", "name": "receiveAToken", "type": "bool"}], "name": "liquidationCall", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

POOL = POOL_POLYGON
CONFIRMATIONS = 16
FLASH_FEE_PCT = 0.05  # допущение по спеке
BONUS_PCT_FLAT = 5.0  # fallback, если reserves cache пуст
WATCHLIST_LIMIT = 5000
DEBT_THRESHOLD_USD = 50.0
GAS_GWEI_ASSUMED = 50.0  # допущение при недоступности живого gas_price
POL_USD_ASSUMED = 2.5  # fallback-допущение; обычно цена POL берётся из оракула Aave
GAS_EST_DEFAULT = 350_000  # типичный liquidationCall gas, fallback
LIQ_LOOKBACK = 2000  # блоков истории LiquidationCall для поиска from-адреса
LIQ_MAX_PROBES = 5  # сколько ликвидаторов проверяем на balanceOf+allowance

# LiquidationCall(collateralAsset idx, debtAsset idx, user idx,
#                 debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken)
# liquidator — третье слово data (НЕ indexed): байты 64..96.
LIQUIDATION_CALL_TOPIC = Web3.keccak(text="LiquidationCall(address,address,address,uint256,uint256,address,bool)")

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]


def _liquidator_from_log(log) -> str | None:
    """Адрес ликвидатора из data события LiquidationCall (слово 3, байты 76..96)."""
    try:
        data = log["data"]
        b = data if isinstance(data, (bytes, bytearray)) else bytes.fromhex(data[2:] if data.startswith("0x") else data)
        if len(b) >= 96:
            return Web3.to_checksum_address("0x" + b[76:96].hex())
    except Exception:
        return None
    return None


def liquidator_from(w3, debt_asset: str, amount: int, safe_block: int, cache: dict):
    """from-адрес для estimateGas: свежий ликвидатор с балансом и approve debtAsset.

    liquidationCall тянет debtAsset с msg.sender (transferFrom), поэтому
    ZERO_ADDRESS/случайный адрес = гарантированный revert. Кэш на debt_asset
    в пределах прохода; None значит «не нашли» — честный gas_assumed fallback.
    """
    if debt_asset in cache:
        return cache[debt_asset]
    addr = None
    try:
        logs = rpc_call(w3, w3.eth.get_logs, {
            "fromBlock": max(0, safe_block - LIQ_LOOKBACK), "toBlock": safe_block,
            "address": POOL, "topics": [LIQUIDATION_CALL_TOPIC]})
        probes = []
        for log in reversed(logs):  # свежие ликвидаторы вероятнее держат approve
            lq = _liquidator_from_log(log)
            if lq and lq not in probes:
                probes.append(lq)
        erc20 = w3.eth.contract(address=debt_asset, abi=ERC20_ABI)
        for lq in probes[:LIQ_MAX_PROBES]:
            if rpc_call(w3, erc20.functions.balanceOf(lq).call) < amount:
                continue
            if rpc_call(w3, erc20.functions.allowance(lq, POOL).call) >= amount:
                addr = lq
                break
    except SystemExit:
        raise
    except Exception:
        addr = None
    cache[debt_asset] = addr
    return addr


def resolve_from_block(args_from_block, safe_block: int) -> int:
    """Явный from_block побеждает; иначе голова - 10000 (0 — валидный ввод)."""
    if args_from_block is not None:
        return args_from_block
    return safe_block - 10000


def resume_from_block(saved, args_from_block, safe_block: int) -> int:
    """Курсор продолжается всегда, даже если отстал больше чем на 10000 блоков.

    Нижняя граница head-10000 применялась раньше и молча пропускала блоки
    между курсором и ней после долгого простоя сервиса.
    """
    if saved is not None and args_from_block is None and saved + 1 <= safe_block:
        return saved + 1
    return resolve_from_block(args_from_block, safe_block)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--from_block", type=int, default=None)
    ap.add_argument("--out", default="logs")
    args = ap.parse_args()
    out_dir = Path(args.out)
    reset_pass()

    w3 = connect(args.rpc)
    pool = w3.eth.contract(address=POOL, abi=POOL_ABI)
    cur_block = get_block_number(w3)
    safe_block = cur_block - CONFIRMATIONS
    cursor_path = out_dir / "cursor.txt"
    saved = load_cursor(cursor_path)
    from_block = resume_from_block(saved, args.from_block, safe_block)
    if from_block > safe_block:
        raise SystemExit(f"INVALID_RANGE: from_block {from_block} > safe_block {safe_block}")
    print(f"scan from_block={from_block} safe_block={safe_block} cur={cur_block}")

    users, last_ok = scan_range(w3, from_block, safe_block)
    print(f"discovery users={len(users)} last_ok_block={last_ok}")

    try:
        block_hash = str(w3.eth.get_block(last_ok).hash.hex())
    except Exception:
        block_hash = "unavailable"

    # Кэш резервов: per-reserve liquidationBonus вместо плоского 5% + цена POL из оракула
    reserve_cache, pol_usd = {}, None
    try:
        reserve_cache, pol_usd = load_reserve_cache(w3)
        print(f"reserves cached: {len(reserve_cache)}")
    except Exception as e:
        print(f"reserves cache failed, flat bonus fallback: {e}")
    if not reserve_cache:
        print("WARNING: reserves cache EMPTY — flat 5% bonus fallback; проверь ABI UiPoolDataProvider живым вызовом")

    # Живая цена газа; цена POL — из оракула Aave, fallback на допущение из спеки.
    # wei -> POL это 1e-18 (1e-9 дал бы gwei: цена за gas-единицу x1e9, gas_usd раздут).
    pol_price = pol_usd if pol_usd is not None else POL_USD_ASSUMED
    try:
        gas_price_wei = w3.eth.gas_price
        gas_usd_per_unit = float(gas_price_wei) * 1e-18 * pol_price
    except Exception:
        gas_usd_per_unit = GAS_GWEI_ASSUMED * 1e-9 * pol_price

    pass_start = int(time.time())
    candidates = {}  # user -> entry (дедуп: свежий HF перезаписывает, без дублей в summary)
    liq_from_cache = {}  # debt_asset -> проверенный from для estimateGas (или None)
    checked = 0
    liquidatable = 0
    net_positive = 0
    sum_net = 0.0
    data_unavailable = 0
    gas_assumed = 0

    # При переполнении вотч-листа вытесняем аккаунты без долга:
    # сортировка по адресу — детерминированная (вместо среза множества).
    watchlist = sorted(users)
    truncated = 0
    if len(watchlist) > WATCHLIST_LIMIT:
        truncated = len(watchlist) - WATCHLIST_LIMIT
        watchlist = watchlist[:WATCHLIST_LIMIT]
        print(f"watchlist truncated: {truncated} addresses dropped (limit {WATCHLIST_LIMIT})")

    try:
        for user in watchlist:
            try:
                # Чтение пиннится к last_ok: block/block_hash в записи — правда,
                # а не «latest через N минут после штампа».
                data = rpc_call(w3, pool.functions.getUserAccountData(user).call, block_identifier=last_ok)
                parsed = parse_account(data)
            except SystemExit:
                raise
            except Exception:
                data_unavailable += 1
                continue
            checked += 1
            hf = parsed["hf"]
            debt_usd = parsed["debt_usd"]
            coll_usd = parsed["collateral_usd"]
            if hf < 1.0 and debt_usd >= DEBT_THRESHOLD_USD:
                liquidatable += 1
                close_pct = close_factor_pct(hf)
                # Fallback-оценка от агрегата; ниже уточняется по топ-активу с кэшем резерва
                debt_to_cover_usd = debt_usd * close_pct / 100.0
                debt_asset = None
                coll_asset = None
                bonus_pct = BONUS_PCT_FLAT
                debt_to_cover_raw = 0
                pos = top_position(w3, user, block_identifier=last_ok)
                if pos is not None:
                    debt_asset, scaled_debt, coll_asset, coll_scaled = pos
                    info = reserve_cache.get(debt_asset)
                    if info:
                        bonus_pct = info["liquidationBonus"]
                        actual_raw = actual_debt_raw(scaled_debt, info["variableBorrowIndex"])
                        price = info["price_in_market_ccy"]
                        frac = close_pct / 100.0  # fallback без данных коллатерала
                        coll_info = reserve_cache.get(coll_asset) if coll_asset else None
                        if price > 0 and coll_info and coll_info["price_in_market_ccy"] > 0:
                            # Точное правило контракта: нужны USD-оценки обоих резервов
                            reserve_debt_usd = token_amount_usd(actual_raw, info["decimals"], price)
                            coll_actual = actual_debt_raw(coll_scaled, coll_info["liquidityIndex"])
                            reserve_coll_usd = token_amount_usd(coll_actual, coll_info["decimals"], coll_info["price_in_market_ccy"])
                            frac = coverable_debt(hf, reserve_debt_usd, reserve_coll_usd, debt_usd)
                        debt_to_cover_raw = actual_raw if frac >= 1.0 else int(actual_raw * frac)
                        if price > 0:
                            # Прибыль с реально покрываемого актива, не со всех долгов сразу
                            debt_to_cover_usd = token_amount_usd(debt_to_cover_raw, info["decimals"], price)
                    else:
                        debt_to_cover_raw = scaled_debt * int(close_pct) // 100  # без index — заниженная оценка
                try:
                    from_addr = liquidator_from(w3, debt_asset, debt_to_cover_raw, safe_block, liq_from_cache) if debt_asset else None
                    if from_addr is not None and debt_to_cover_raw > 0:
                        liq_fn = pool.functions.liquidationCall(coll_asset or debt_asset, debt_asset, user, debt_to_cover_raw, False)
                        gas_est = rpc_call(w3, liq_fn.estimate_gas, {"from": from_addr, "value": 0}, block_identifier=last_ok)
                        gas_usd = float(gas_est) * gas_usd_per_unit
                        flag = None
                    else:
                        raise ValueError("no viable from address for estimate")
                except SystemExit:
                    raise
                except Exception:
                    gas_usd = GAS_EST_DEFAULT * gas_usd_per_unit
                    flag = "gas_assumed"
                    gas_assumed += 1
                npv = net_profit(debt_to_cover_usd, bonus_pct, FLASH_FEE_PCT, gas_usd)
                entry = {"user": user, "hf": hf, "debt_usd": debt_usd, "collateral_usd": coll_usd,
                         "net_profit_usd": npv, "ts": int(time.time()), "block": last_ok,
                         "block_hash": block_hash,
                         "debt_asset": debt_asset,
                         "bonus_pct": bonus_pct,
                         "gas_usd": gas_usd}
                if flag:
                    entry["flag"] = flag
                # Если кандидат уже в списке (дубль по user в этом проходе) — свежий HF перезаписывает,
                # а не добавляется второй раз. net_positive/sum пересчитываем со свежими цифрами.
                log_entry(entry, out_dir)  # JSONL dedup
                if user in candidates:
                    old_net = candidates[user].get("net_profit_usd", 0.0)
                    if old_net > 0:
                        net_positive -= 1
                        sum_net -= old_net
                candidates[user] = entry
                if npv > 0:
                    net_positive += 1
                    sum_net += npv
    except SystemExit:
        # RPC_DOWN посреди опроса: курсор уже покрытой discovery сохраняем,
        # иначе следующий проход переделывает весь скан логов заново.
        try:
            save_cursor(last_ok, cursor_path)
            print(f"cursor saved at {last_ok} before RPC_DOWN")
        except Exception:
            pass
        raise

    pass_end = int(time.time())
    s = {"pass_start_ts": pass_start, "pass_end_ts": pass_end, "candidates_checked": checked,
         "liquidatable": liquidatable, "net_positive": net_positive,
         "sum_net_profit_usd": round(sum_net, 4), "data_unavailable": data_unavailable,
         "gas_assumed": gas_assumed,
         "rl_hits": get_rl_hits(), "last_ok_block": last_ok, "watchlist_truncated": truncated,
         "reserve_cache_size": len(reserve_cache),
         "pol_price_usd": pol_price, "pol_price_source": "oracle" if pol_usd is not None else "assumed",
         "candidates": list(candidates.values())}
    # Persist курсора: следующий проход продолжит с last_ok + 1
    try:
        save_cursor(last_ok, cursor_path)
    except Exception as e:
        print(f"cursor save failed: {e}")
    p = summary(s, out_dir)
    print(f"PASS candidates_checked={checked} liquidatable={liquidatable} net_positive={net_positive} data_unavailable={data_unavailable} gas_assumed={gas_assumed} rl_hits={get_rl_hits()}")
    print(f"WROTE {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
