"""tests/test_scan.py — оффлайн-регрессии на ABI и курсор from_block.

Критично: liquidationCall Aave V3 Pool принимает 5 аргов (receiveAToken);
4-арг ABI давал чужой selector -> estimateGas всегда ревертил -> gas_assumed.
"""
from web3 import Web3

from scanner.scan import POOL_ABI, resolve_from_block, resume_from_block


def _liquidation_call():
    return next(f for f in POOL_ABI if f.get("name") == "liquidationCall")


def test_liquidation_call_abi_matches_pool_signature():
    fn = _liquidation_call()
    assert [i["type"] for i in fn["inputs"]] == ["address", "address", "address", "uint256", "bool"]


def test_liquidation_call_selector_is_canonical_v3():
    c = Web3().eth.contract(address="0x" + "11" * 20, abi=POOL_ABI)
    calldata = c.encode_abi("liquidationCall", args=["0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20, 1_000_000, False])
    canonical = Web3.keccak(text="liquidationCall(address,address,address,uint256,bool)")[:4]
    assert calldata[:10] == "0x" + canonical.hex()  # 0x + 8 hex-цифр selector-а
    assert len(Web3.to_bytes(hexstr=calldata)) == 4 + 32 * 5


def test_resolve_from_block_zero_is_valid():
    assert resolve_from_block(0, 1000) == 0
    assert resolve_from_block(None, 1000) == 1000 - 10000
    assert resolve_from_block(777, 1000) == 777


def test_cursor_resume_no_gap_after_long_downtime():
    # Курсор отстал на 50000 блоков (> head-10000): продолжаем с курсора,
    # а не прыгаем на head-10000 с пропуском блоков.
    safe = 1_000_000
    assert resume_from_block(saved=950_000, args_from_block=None, safe_block=safe) == 950_001
    # Курсор внутри обычного окна — тоже продолжается
    assert resume_from_block(saved=999_000, args_from_block=None, safe_block=safe) == 999_001


def test_explicit_from_block_beats_cursor_and_no_cursor_default():
    safe = 1_000_000
    assert resume_from_block(saved=950_000, args_from_block=777, safe_block=safe) == 777
    assert resume_from_block(saved=None, args_from_block=None, safe_block=safe) == safe - 10000
    # Курсор впереди safe_block (реорг/часы) — не идём назад, дефолтное окно
    assert resume_from_block(saved=safe + 5, args_from_block=None, safe_block=safe) == safe - 10000
