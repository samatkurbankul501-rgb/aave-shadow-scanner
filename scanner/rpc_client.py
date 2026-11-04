"""rpc_client: подключение к RPC Polygon + rate limit/backoff.

Из спеки: connect(url)->Web3, rpc_call(fn, args) -> result, get_block_number()->int.
Поведение: 429 -> backoff минимум 5с, инкремент rl_hits;
3 подряд -> SystemExit('RPC_DOWN'). Timeout -> 1 ретрай 2с.
Никаких приватных ключей.

Исправления (2026-11-04): get_block_number через свойство w3.eth.block_number
(метода get_block_number у web3.py нет); rl_hits считается подряд, сброс при успехе.
"""
import time
from web3 import Web3

_rl_hits_total = 0
_rl_hits_consec = 0


def connect(url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        print("RPC_DOWN: нет соединения")
        raise SystemExit("RPC_DOWN")
    return w3


def _is_429(msg: str) -> bool:
    return "429" in msg or "ratelimit" in msg or "too many" in msg


def _retry_after_seconds(msg: str) -> float:
    # В сообщении web3 заголовки недоступны; пробуем вытащить Retry-After
    # как число из текста, иначе минимум 5с по спеке.
    import re
    m = re.search(r"retry[- ]after[^0-9]*(\d+)", msg, re.IGNORECASE)
    if m:
        return max(5.0, float(m.group(1)))
    return 5.0


def rpc_call(w3: Web3, fn, *args, **kwargs) -> object:
    """Вызов RPC-метода с backoff на 429 и ретраем на timeout."""
    global _rl_hits_total, _rl_hits_consec
    for attempt in range(4):
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
                if attempt == 0:
                    time.sleep(2)
                    continue
                print("RPC_DOWN: timeout")
                raise SystemExit("RPC_DOWN")
            raise
    return None


def get_block_number(w3: Web3) -> int:
    return rpc_call(w3, lambda: w3.eth.block_number)


def get_rl_hits() -> int:
    return _rl_hits_total
