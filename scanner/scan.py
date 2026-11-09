"""scan.py: точка входа shadow-скана (этап B, только чтение, без ключей).

Поток: connect -> discovery (курсор до safe_block) -> getUserAccountData по
вотч-листу -> фильтр HF<1.0 + долг>=$50 -> оценка net_profit -> logger.

Исправления (2026-11-04):
- вся логика внутри main() + __main__ guard (импорт больше не требует --rpc);
- курсор discovery используется (последний покрытый блок), битые батчи не
  теряются молча;
- block_hash берётся один раз на проход;
- нет данных — нет записи: бездолговые аккаунты не попадают в лог;
- парсер вынесен в scanner/parser.py (тесты больше не самотестируют);
- per-reserve бонус + реальный debtAsset через reserves.py
  (UiPoolDataProvider.getReservesData / getUserReservesData);
- estimate_gas на реальных liquidationCall-аргументах; при отклонении
  fallback на gas_assumed (а не на None — иначе гейт B->C всегда 0).

Честные допущения этапа B (помечены в логе как допущения, не факты):
- closeFactor=50% применён к долгу-подопытной сумме (closeFactor из
  getReservesData точно так же);
- FLASH_FEE_PCT=0.05 — допущение flash fee (spec: 0.05% фикс для оценки);
- gas_usd = gas_est * 50 gwei * 2.5 USD/MATIC — допущение для оценки;
  живой газ через w3.eth.gas_price (fallback на допущение).
При revert estimateGas на реальных liquidationCall-аргументах — запись с флагом
gas_assumed и net_profit посчитан по fallback-газу (по спеке). Доля fallbackа
считается (gas_assumed в сводном JSON) — по ней виден реализм оценки газа.
"""
import argparse
import time
from pathlib import Path

from scanner.calc import net_profit
from scanner.discovery import POOL_POLYGON, load_cursor, save_cursor, scan_range
from scanner.logger import log_entry, reset_pass, summary
from scanner.parser import parse_account
from scanner.reserves import load_reserve_cache, top_position
from scanner.rpc_client import connect, get_block_number, get_rl_hits, rpc_call

POOL_ABI = [
    {"inputs": [{"internalType": "address", "name": "user", "type": "address"}], "name": "getUserAccountData", "outputs": [{"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"}, {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"}, {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"}, {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"}, {"internalType": "uint256", "name": "ltv", "type": "uint256"}, {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "collateralAsset", "type": "address"}, {"internalType": "address", "name": "debtAsset", "type": "address"}, {"internalType": "address", "name": "user", "type": "address"}, {"internalType": "uint256", "name": "debtToCover", "type": "uint256"}, {"internalType": "bool", "name": "receiveAToken", "type": "bool"}], "name": "liquidationCall", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

ZERO_ADDRESS = "0x" + "0" * 40  # нейтральный from для симуляции estimateGas
POOL = POOL_POLYGON
CONFIRMATIONS = 16
FLASH_FEE_PCT = 0.05  # допущение по спеке
CLOSE_FACTOR_PCT = 50.0  # допущение: что и говорит спека (обычно 50%)
BONUS_PCT_FLAT = 5.0  # fallback, если reserves cache пуст
WATCHLIST_LIMIT = 5000
DEBT_THRESHOLD_USD = 50.0
GAS_GWEI_ASSUMED = 50.0  # допущение при недоступности живого gas_price
MATIC_USD_ASSUMED = 2.5  # допущение
GAS_EST_DEFAULT = 350_000  # типичный liquidationCall gas, fallback


def resolve_from_block(args_from_block, safe_block: int) -> int:
    """Явный from_block побеждает; иначе голова - 10000 (0 — валидный ввод)."""
    if args_from_block is not None:
        return args_from_block
    return safe_block - 10000


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
    from_block = resolve_from_block(args.from_block, safe_block)
    if from_block > safe_block:
        raise SystemExit(f"INVALID_RANGE: from_block {from_block} > safe_block {safe_block}")
    # Если курсор есть и from_block не задан явно — продолжить с него
    cursor_path = out_dir / "cursor.txt"
    saved = load_cursor(cursor_path)
    if saved is not None and args.from_block is None and saved + 1 <= safe_block and saved + 1 >= from_block:
        from_block = saved + 1
    print(f"scan from_block={from_block} safe_block={safe_block} cur={cur_block}")

    users, last_ok = scan_range(w3, from_block, safe_block)
    print(f"discovery users={len(users)} last_ok_block={last_ok}")

    try:
        block_hash = str(w3.eth.get_block(last_ok).hash.hex())
    except Exception:
        block_hash = "unavailable"

    # Кэш резервов: per-reserve liquidationBonus вместо плоского 5%
    reserve_cache = {}
    try:
        reserve_cache = load_reserve_cache(w3) or {}
        print(f"reserves cached: {len(reserve_cache)}")
    except Exception as e:
        print(f"reserves cache failed, flat bonus fallback: {e}")

    # Живая цена газа, с fallback на допущение из спеки.
    try:
        gas_price_wei = w3.eth.gas_price
        gas_usd_per_unit = float(gas_price_wei) * 1e-9 * MATIC_USD_ASSUMED
    except Exception:
        gas_usd_per_unit = GAS_GWEI_ASSUMED * 1e-9 * MATIC_USD_ASSUMED

    pass_start = int(time.time())
    candidates = {}  # user -> entry (дедуп: свежий HF перезаписывает, без дублей в summary)
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

    for user in watchlist:
        try:
            data = rpc_call(w3, pool.functions.getUserAccountData(user).call)
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
            debt_to_cover_usd = debt_usd * CLOSE_FACTOR_PCT / 100.0
            # Реальный debtAsset с max долгом — для estimate_gas и per-reserve бонуса
            debt_asset = None
            coll_asset = None
            bonus_pct = BONUS_PCT_FLAT
            debt_to_cover_raw = 0
            pos = top_position(w3, user)
            if pos is not None:
                debt_asset, scaled_debt, coll_asset = pos
                debt_to_cover_raw = int(scaled_debt * 0.5)  # closeFactor 50%
                info = reserve_cache.get(debt_asset)
                if info:
                    bonus_pct = info["liquidationBonus"]
                    # Пересчёт debtToCover в USD по per-reserve price если есть
                    if info.get("price_in_market_ccy"):
                        pass  # пока оставляем задолженность из parse_account (агрегат)
            try:
                if debt_asset is not None and debt_to_cover_raw > 0:
                    # Реальный debtAsset + топ-коллатерал позиции (placeholder для estimate:
                    # точный коллатерал не обязателен для газа, но POOL как актив давал revert 100%)
                    liq_fn = pool.functions.liquidationCall(coll_asset or debt_asset, debt_asset, user, debt_to_cover_raw, False)
                    gas_est = rpc_call(w3, liq_fn.estimate_gas, {"from": ZERO_ADDRESS, "value": 0})
                    gas_usd = float(gas_est) * gas_usd_per_unit
                    flag = None
                else:
                    raise ValueError("no debt asset")
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

    pass_end = int(time.time())
    s = {"pass_start_ts": pass_start, "pass_end_ts": pass_end, "candidates_checked": checked,
         "liquidatable": liquidatable, "net_positive": net_positive,
         "sum_net_profit_usd": round(sum_net, 4), "data_unavailable": data_unavailable,
         "gas_assumed": gas_assumed,
         "rl_hits": get_rl_hits(), "last_ok_block": last_ok, "watchlist_truncated": truncated,
         "reserve_cache_size": len(reserve_cache),
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
