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
  from — реальный ликвидатор из свежих LiquidationCall-логов с проверкой
  balanceOf + allowance (liquidator_from); ZERO_ADDRESS = гарантированный revert.
- При revert estimateGas / отсутствии ликвидатора: gas_assumed + fallback-газ
  GAS_EST_DEFAULT; доля fallbackа — gas_assumed в сводном JSON, не прятать.
- closeFactor по контракту (LiquidationLogic): 100% долга резерва по умолчанию;
  cap = 50% ОБЩЕГО долга только при HF СТРОГО > 0.95 И долг резерва >= $2000
  И коллатерал резерва >= $2000 (calc.coverable_debt). HF == 0.95 -> 100%.
  Без данных коллатерала — fallback close_factor_pct(hf). Коллатерал в USD
  считается из scaledATokenBalance * liquidityIndex // RAY (не variableBorrowIndex).
- debtToCover — с топ-ОДНОГО debt-актива: scaledDebt * variableBorrowIndex // RAY
  (целочисленно), USD через per-reserve price/decimals; агрегат — только fallback.
- Чтения пользователя (getUserAccountData/getUserReservesData/estimateGas)
  пиннятся к last_ok (block_identifier): block/block_hash в JSONL — правда.
- gas_usd: gas_price_wei * 1e-18 * цена POL (wei->POL!). Не 1e-9 — это gwei,
  цена за gas-единицу раздувается x1e9 и гейт всегда «денег нет».
  Цена POL — из оракула Aave (getReservesData base tuple), fallback POL_USD_ASSUMED.
- connect проверяет chain_id == 137 (WRONG_CHAIN): чужая сеть с Polygon-адресом
  Pool даёт молчаливый 100% data_unavailable.
- logger: имена файлов с uuid-суффиксом — два прохода в одну секунду не делят
  JSONL (append дал бы дубли по user).
- RPC_DOWN посреди опроса вотч-листа: курсор covered discovery сохраняется.
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
Тесты: python -m pytest tests/ -q (39 шт, из них 6 discovery, 9 rpc_client, 9 calc).
