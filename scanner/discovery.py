"""discovery: скан логов Pool Aave V3 (Borrow, Supply) -> set[address].

Из спеки: scan_range(from_block, to_block) -> set[address] + курсор частичного
прохода (последний успешно покрытый блок). Батчи по 20 блоков. Использует
eth_getLogs напрямую (без create_filter, который требует eth_newFilter и
недоступен на бесплатных RPC).

Сигнатуры (фактические, Aave V3 IPool.sol):
  Borrow(address indexed reserve, address user, address indexed onBehalfOf,
         uint256 amount, uint256 interestRateMode, uint256 borrowRate,
         uint16 indexed referralCode)
  Supply(address indexed reserve, address user, address indexed onBehalfOf,
         uint256 amount, uint16 indexed referralCode)
Поле user в обоих событиях НЕ indexed — он в data[0]. onBehalfOf индексирован
и лежит в topics[2]; именно на нём держится позиция (aToken/debtToken),
его и возвращаем.
"""
from pathlib import Path
from web3 import Web3

POOL_POLYGON = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
POOL_AMOY = None  # После проверки 2026-11-04: Aave V3 на Amoy не задеплоен в aave-address-book
                 # (нет AaveV3Amoy.ts). Амой-смоук требует либо свой деплой, либо Sepolia.
                 # Не подставлять Polygon-адрес: амой-смоук с ним молча даст пустоту.
POOLS = {"polygon": POOL_POLYGON, "amoy": POOL_AMOY}

# Сигнатуры событий Aave V3 Pool (полные, с onBehalfOf)
SIG_BORROW = "Borrow(address,address,address,uint256,uint256,uint256,uint16)"
SIG_SUPPLY = "Supply(address,address,address,uint256,uint16)"
TOPIC_BORROW = Web3.keccak(text=SIG_BORROW)
TOPIC_SUPPLY = Web3.keccak(text=SIG_SUPPLY)
TOPIC_WITHDRAW = Web3.keccak(text="Withdraw(address,address,address,uint256)")

ABI = [
    {"anonymous": False, "inputs": [{"indexed": True, "name": "reserve", "type": "address"}, {"indexed": False, "name": "user", "type": "address"}, {"indexed": True, "name": "onBehalfOf", "type": "address"}, {"indexed": False, "name": "amount", "type": "uint256"}, {"indexed": False, "name": "interestRateMode", "type": "uint256"}, {"indexed": False, "name": "borrowRate", "type": "uint256"}, {"indexed": True, "name": "referralCode", "type": "uint16"}], "name": "Borrow", "type": "event"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "reserve", "type": "address"}, {"indexed": False, "name": "user", "type": "address"}, {"indexed": True, "name": "onBehalfOf", "type": "address"}, {"indexed": False, "name": "amount", "type": "uint256"}, {"indexed": True, "name": "referralCode", "type": "uint16"}], "name": "Supply", "type": "event"},
]


def _user_from_log(log) -> str | None:
    """onBehalfOf — topics[2] (32 байта, последние 20 — адрес)."""
    try:
        topics = log["topics"]
        if len(topics) >= 3:
            return Web3.to_checksum_address("0x" + topics[2].hex()[-40:])
    except Exception:
        return None
    return None


def save_cursor(last_ok: int, path: Path) -> None:
    """Персистентный курсор: пишет last_ok в файл (атомарно)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(last_ok), encoding="utf-8")
    tmp.replace(path)


def load_cursor(path: Path) -> int | None:
    """Читает курсор, None если файл отсутствует/бит."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def scan_range(w3, from_block: int, to_block: int, pool_addr: str = POOL_POLYGON, batch: int = 20):
    """Возвращает (set[address], last_ok_block).

    last_ok_block — последний блок, покрытый обоими запросами getLogs.
    При ошибке на середине диапазона возвращает частичный результат и
    курсор; следующий проход продолжает с last_ok_block + 1.
    """
    from scanner.rpc_client import rpc_call
    users = set()
    last_ok = from_block - 1
    cur = from_block
    while cur <= to_block:
        end = min(cur + batch - 1, to_block)
        batch_ok = True
        for topic in (TOPIC_BORROW, TOPIC_SUPPLY):
            try:
                logs = rpc_call(w3, w3.eth.get_logs, {"fromBlock": cur, "toBlock": end, "address": pool_addr, "topics": [topic]})
                for log in logs:
                    addr = _user_from_log(log)
                    if addr:
                        users.add(addr)
            except SystemExit:
                raise
            except Exception as e:
                print(f"getLogs {cur}-{end} failed: {e}; stops at block {cur - 1}")
                batch_ok = False
                break
        if not batch_ok:
            break
        last_ok = end
        cur = end + 1
    return users, last_ok


if __name__ == "__main__":
    import argparse
    from scanner.rpc_client import connect, get_block_number
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--from", dest="from_block", type=int, required=True)
    ap.add_argument("--to", type=int, required=False, default=None)
    ap.add_argument("--pool", required=False, default=POOL_POLYGON)
    args = ap.parse_args()
    w3 = connect(args.rpc)
    to_block = args.to if args.to is not None else get_block_number(w3)
    users, last_ok = scan_range(w3, args.from_block, to_block, args.pool)
    print(f"OK users={len(users)} last_ok={last_ok}")
