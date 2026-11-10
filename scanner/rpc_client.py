"""rpc_client: подключение к RPC Polygon + rate limit/backoff.

Из спеки: connect(url)->Web3, rpc_call(w3, fn, *args) -> result, get_block_number(w3)->int.
Поведение: 429 -> backoff минимум 5с, инкремент rl_hits;
3 подряд -> SystemExit('RPC_DOWN'). Timeout -> 1 ретрай 2с (независимо от
номера попытки: 429-ретрай не съедает timeout-ретрай).
connect проверяет chain_id == 137: чужая сеть с Polygon-адресом Pool дала бы
молчаливый 100% data_unavailable. Никаких приватных ключей.
"""
import re
import time

from web3 import Web3

POLYGON_CHAIN_ID = 137

_rl_hits_total = 0
_rl_hits_consec = 0
_LAST_CALL_TS = 0.0
_MIN_INTERVAL = 0.1  # 10 req/s

# \b не даёт ложного срабатывания на номерах блоков вида 429123
_RATE_LIMIT_RE = re.compile(r"\b429\b|ratelimit|rate[- ]limit|too many", re.IGNORECASE)


def verify_chain(w3: Web3) -> None:
    chain_id = rpc_call(w3, lambda: w3.eth.chain_id)
    if chain_id != POLYGON_CHAIN_ID:
        raise SystemExit(f"WRONG_CHAIN: expected Polygon {POLYGON_CHAIN_ID}, got {chain_id}")


def connect(url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        print("RPC_DOWN: нет соединения")
        raise SystemExit("RPC_DOWN")
    verify_chain(w3)
    return w3


def _is_429(msg: str) -> bool:
    return bool(_RATE_LIMIT_RE.search(msg))


def _retry_after_seconds(msg: str) -> float:
    m = re.search(r"retry[- ]after[^0-9]*(\d+)", msg, re.IGNORECASE)
    if m:
        return max(5.0, float(m.group(1)))
    return 5.0


def _throttle() -> None:
    global _LAST_CALL_TS
    now = time.time()
    gap = _MIN_INTERVAL - (now - _LAST_CALL_TS)
    if gap > 0:
        time.sleep(gap)
    _LAST_CALL_TS = time.time()


def rpc_call(w3: Web3, fn, *args, **kwargs) -> object:
    """Вызов RPC-метода с throttle, backoff на 429 и ретраем на timeout."""
    global _rl_hits_total, _rl_hits_consec
    timeout_retried = False
    for _attempt in range(4):
        _throttle()
        try:
            result = fn(*args, **kwargs)
            _rl_hits_consec = 0
            return result
        except Exception as e:
            msg = str(e).lower()
            if _is_429(msg):
                _rl_hits_total += 1
                _rl_hits_consec += 1
                if _rl_hits_consec >= 3:
                    print("RPC_DOWN: rate limited 3 раза подряд")
                    raise SystemExit("RPC_DOWN")
                time.sleep(_retry_after_seconds(msg))
                continue
            if "timeout" in msg or "timed out" in msg or "connection" in msg:
                if not timeout_retried:
                    timeout_retried = True
                    time.sleep(2)
                    continue
                print("RPC_DOWN: timeout")
                raise SystemExit("RPC_DOWN")
            raise
    raise SystemExit("RPC_DOWN: retries exhausted")


def get_block_number(w3: Web3) -> int:
    return rpc_call(w3, lambda: w3.eth.block_number)


def get_rl_hits() -> int:
    return _rl_hits_total