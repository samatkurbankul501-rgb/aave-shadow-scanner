# Aave V3 Shadow Scanner (этап B)

Read-only сканер ликвидаций Aave V3 на Polygon. Без ключей и транзакций.
Python 3.11, web3==8.*, pytest. Маршрут: B (этот код) → C (тестнет) → A (mainnet).
Этап C: Sepolia или свой деплой — Aave V3 на Amoy не задеплоен, POOL_AMOY=None.

## Инварианты (не ломать)

- ABI liquidationCall — 5 аргов (receiveAToken); каноничный selector закреплён
  в tests/test_scan.py. 4-арг ABI = чужой selector, estimateGas всегда ревертит.
- reserveLiquidationBonus — bps-множитель (10500 = +5%), вычитать 10000 перед /100.
- estimate_gas: реальный debtAsset + топ-коллатерал позиции через
  reserves.top_position (см. scan.py). Не подставлять POOL как актив.
- При revert estimateGas: gas_assumed + fallback-газ GAS_EST_DEFAULT;
  доля fallbackа — gas_assumed в сводном JSON, не прятать.
- discovery: курсор = covered_end (фактически покрытый конец диапазона),
  не запрошенный end батча. Батч по умолчанию 200, сжатие пополам до MIN_BATCH=5.
- resume: курсор продолжается всегда (resume_from_block), даже если отстал
  больше чем на 10000 блоков — иначе после долгого простоя блоки пропускаются.
  Явный --from_block побеждает курсор.
- logger dedup: в JSONL ровно одна строка на user, ВСЕ поля свежие
  (hf/ts/net_profit_usd; полный merge, см. test_reserves.py::test_logger_dedup).

## Деплой

deploy/setup.sh клонирует git HEAD. Коммитить перед деплоем, иначе на сервер
поедет устаревший код (баги selector/бонуса/from_block=0 жили в дереве).

## Спеки

Синхронизировать с кодом: сигнатура net_profit(debt_usd, bonus_pct, fee_pct, gas_usd),
parse_account len==6, UiPoolDataProvider вместо getUserConfigurationMap,
батч 200 адаптивный. Проверять grep'ом перед каждой правкой спеки.
Тесты: python -m pytest tests/ -q (18 шт, из них 5 discovery).
