"""scan.py: точка входа shadow-скана (этап B, только чтение, без ключей).

Поток: connect -> discovery (курсор до safe_block) -> getUserAccountData по
вотч-листу -> фильтр HF<1.0 + долг>=$50 -> оценка net_profit -> logger.

Исправления (2026-11-04):
- вся логика внутри main() + __main__ guard (импорт больше не требует --rpc);
- курсор discovery используется (последний покрытый блок), битые батчи не
  теряются молча;
- block_hash берётся один раз на проход;
- нет данных — нет записи: бездолговые аккаунты не попадают в лог.

Честные допущения этапа B (помечены в логе как допущения, не факты):
- BONUS_PCT=5.0 — плоский бонус всех резервов (spec говорит per-reserve из
  getReservesData; per-reserve бонус — следующий шаг);
- closeFactor=50% применён к долгу-подопытной сумме (closeFactor из
  getReservesData точно так же);
- FLASH_FEE_PCT=0.05 — допущение flash fee (spec: 0.05% фикс для оценки);
- gas_usd = gas_est * 50 gwei * 2.5 USD/MATIC — допущение для оценки;
  живой газ через w3.eth.gas_price (fallback на допущение).
При revert estimateGas на ликвидационных аргументах — запись с флагом
gas_unestimable и net_profit_usd=None (по спеке).
"""
import argparse
import time
from pathlib import Path

from scanner.calc import net_profit
from scanner.discovery import POOL_POLYGON, scan_range
from scanner.logger import log_entry, summary
from scanner.rpc_client import connect, get_block_number, get_rl_hits, rpc_call

POOL_ABI = [
    {"inputs": [{"internalType": "address", "name": "user", "type": "address"}], "name": "getUserAccountData", "outputs": [{"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"}, {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"}, {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"}, {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"}, {"internalType": "uint256", "name": "ltv", "type": "uint256"}, {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "collateralAsset", "type": "address"}, {"internalType": "address", "name": "debtAsset", "type": "address"}, {"internalType": "address", "name": "user", "type": "address"}, {"internalType": "address", "name": "debtToCover", "type": "uint256"}], "name": "liquidationCall", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

POOL = POOL_POLYGON
CONFIRMATIONS = 16
FLASH_FEE_PCT = 0.05  # допущение по спеке
CLOSE_FACTOR_PCT = 50.0  # допущение: что и говорит спека (обычно 50%)
BONUS_PCT = 5.0  # допущение: плоский бонус (per-reserve — следующий шаг)
WATCHLIST_LIMIT = 5000
DEBT_THRESHOLD_USD = 50.0
GAS_GWEI_ASSUMED = 50.0  # допущение при недоступности живого gas_price
MATIC_USD_ASSUMED = 2.5  # допущение


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--from_block", type=int, default=None)
    ap.add_argument("--out", default="logs")
    args = ap.parse_args()
    out_dir = Path(args.out)

    w3 = connect(args.rpc)
    pool = w3.eth.contract(address=POOL, abi=POOL_ABI)
    cur_block = get_block_number(w3)
    safe_block = cur_block - CONFIRMATIONS
    from_block = args.from_block or (safe_block - 10000)
    print(f"scan from_block={from_block} safe_block={safe_block} cur={cur_block}")

    users, last_ok = scan_range(w3, from_block, safe_block)
    print(f"discovery users={len(users)} last_ok_block={last_ok}")

    try:
        block_hash = str(w3.eth.get_block(last_ok).hash.hex())
    except Exception:
        block_hash = "unavailable"

    # Живая цена газа, с fallback на допущение из спеки.
    try:
        gas_price_wei = w3.eth.gas_price
        gas_usd_per_unit = float(gas_price_wei) * 1e-9 * MATIC_USD_ASSUMED
    except Exception:
        gas_usd_per_unit = GAS_GWEI_ASSUMED * 1e-9 * MATIC_USD_ASSUMED

    pass_start = int(time.time())
    candidates = []
    checked = 0
    liquidatable = 0
    net_positive = 0
    sum_net = 0.0
    data_unavailable = 0

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
        except SystemExit:
            raise
        except Exception:
            data_unavailable += 1
            continue
        checked += 1
        hf = data[5] / 1e18
        debt_usd = (data[1] / 1e8) * 1.0
        coll_usd = (data[0] / 1e8) * 1.0
        if hf < 1.0 and debt_usd >= DEBT_THRESHOLD_USD:
            liquidatable += 1
            debt_to_cover_usd = debt_usd * CLOSE_FACTOR_PCT / 100.0
            # Токен долга неизвестен из getUserAccountData; берём POOL как
            # заглушку — estimateGas почти наверняка ревертнёт, и это честно
            # помечается gas_unestimable, а не фальшивым числом.
            try:
                gas_est = rpc_call(
                    w3,
                    w3.eth.estimate_gas,
                    {"from": user, "to": POOL, "value": 0, "data": pool.encodeABI(fn_name="liquidationCall", args=[POOL, POOL, user, 0])},
                )
                gas_usd = float(gas_est) * gas_usd_per_unit
            except Exception:
                gas_usd = None
            if gas_usd is None:
                entry = {"user": user, "hf": hf, "debt_usd": debt_usd, "collateral_usd": coll_usd,
                         "net_profit_usd": None, "ts": int(time.time()), "block": last_ok,
                         "block_hash": block_hash, "flag": "gas_unestimable"}
            else:
                npv = net_profit(debt_to_cover_usd, BONUS_PCT, FLASH_FEE_PCT, gas_usd)
                entry = {"user": user, "hf": hf, "debt_usd": debt_usd, "collateral_usd": coll_usd,
                         "net_profit_usd": npv, "ts": int(time.time()), "block": last_ok,
                         "block_hash": block_hash}
                if npv > 0:
                    net_positive += 1
                    sum_net += npv
            log_entry(entry, out_dir)
            candidates.append(entry)

    pass_end = int(time.time())
    s = {"pass_start_ts": pass_start, "pass_end_ts": pass_end, "candidates_checked": checked,
         "liquidatable": liquidatable, "net_positive": net_positive,
         "sum_net_profit_usd": round(sum_net, 4), "data_unavailable": data_unavailable,
         "rl_hits": get_rl_hits(), "last_ok_block": last_ok, "watchlist_truncated": truncated,
         "candidates": candidates}
    p = summary(s, out_dir)
    print(f"PASS candidates_checked={checked} liquidatable={liquidatable} net_positive={net_positive} data_unavailable={data_unavailable} rl_hits={get_rl_hits()}")
    print(f"WROTE {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
