# Aave V3 Shadow Scanner (этап B)

Read-only сканер ликвидаций Aave V3 на Polygon. Без ключей и транзакций.
Python 3.11, web3==8.*, pytest. Маршрут: B (этот код) → C (тестнет) → A (mainnet).
Этап C: Sepolia или свой деплой — Aave V3 на Amoy не задеплоен, POOL_AMOY=None.

## Инварианты (не ломать)

- ABI liquidationCall — 5 аргов (receiveAToken); каноничный selector закреплён
  в tests/test_scan.py. 4-арг ABI = чужой selector, estimateGas всегда ревертит.
- reserveLiquidationBonus — bps-множитель (10500 = +5%), вычитать 10000 перед /100.
- Бонус в модели — NET после liquidationProtocolFee (LiquidationLogic: fee
  срезает часть бонус-коллатерала): net = gross*(1 - fee_bps/10000),
  calc.net_liquidation_bonus. fee — биты 152-167 getConfiguration (маска 0xFFFF).
  Источник бонуса — КОЛЛАТЕРАЛЬНЫЙ резерв позиции (bonus_src = coll_info or
  debt_info), не debt-резерв. fee не прочитан -> gross + liq_fee_unreadable
  в сводном; в JSONL liquidation_protocol_fee_bps. Живые: USDC/WETH net 4.5%,
  WPOL net 6.3% (fee 1000 bps = 10% бонуса).
- eMode: при категории юзера != 0 и коллатерале в collateralBitmap категории
  бонус = бонус КАТЕГОРИИ (getEModeCategoryData, gross), не резерва
  (развёрнутый LiquidationLogic:226-239). Fee остаётся от коллатерального
  резерва. Членство: бит (1 << reserve id), id = индекс в getReservesList.
  Категория не прочитана -> резервный бонус + emode_unreadable в сводном,
  emode_bonus_unreadable в JSONL. top_position возвращает категорию 5-м полем.
- Топ debt/collateral ранжируются по USD через кэш резервов (scaled-балансы
  разных активов несравнимы: decimals/цена). Без кэша — legacy по scaled.
- Покрытие долга — calc.executable_cover_usd: closeFactor cap + достаточность
  коллатерала (collateral/(1+gross), контракт урезает долг и изымает весь
  коллатерал) + анти-пылевое правило MIN_LEFTOVER_BASE=$1000 (частичная
  ликвидация обязана оставить >= $1000 и долга, и коллатерала, иначе revert
  MustNotLeaveDust; запрос урезается до исполнимого). В JSONL cover_limit
  (collateral|dust) и debt_to_cover_usd.
- Резервы inactive/paused (кэш) или в grace-периоде (getLiquidationGracePeriod
  > timestamp блока) -> кандидат блокируется (reserve_blocked в сводном),
  ликвидация по ним ревертит (ValidationLogic).
- Вотч-лист ПЕРСИСТИТСЯ между проходами (watchlist.txt, атомарно): discovery
  даёт только новых юзеров окна, персистентные перепроверяются каждый проход
  (HF дрейфует ниже 1 без новых событий). Бездолговые (debt_usd==0) вычищаются
  после опроса; data_unavailable остаются. При RPC_DOWN вотч-лист сохраняется
  вместе с курсором. Иначе гейт систематически недосчитывает кандидатов.
- UIDP_ABI getReservesData — РОВНО 40 полей AggregatedReserveData, сверено
  с верифицированным деплоем (Sourcify, chain 137). Лишнее поле (virtualAccActive,
  41 поле) ломает decode целиком -> пустой кэш -> flat fallback. Регрессия:
  test_reserves.py::test_uidp_abi_matches_deployed_layout.
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
- net_profit учитывает swap изъятого коллатерала обратно в debt-актив:
  debt*(1+bonus/100)*(1-swap_cost/100) - debt*(1+flash_fee/100) - gas.
  swap_cost_pct — допущение этапа B (SWAP_COST_PCT_ASSUMED=0.3, комиссия DEX;
  slippage не моделируется), видно в JSONL (swap_cost_pct) и сводном
  (swap_cost_pct_assumed). swap_cost_pct=0 = старая формула (спека $9.89).
- Чтения пользователя (getUserAccountData/getUserReservesData/estimateGas)
  пиннятся к last_ok (block_identifier): block/block_hash в JSONL — правда.
- gas_usd: gas_price_wei * 1e-18 * цена POL (wei->POL!). Не 1e-9 — это gwei,
  цена за gas-единицу раздувается x1e9 и гейт всегда «денег нет».
  Цена POL — из оракула Aave (getReservesData base tuple), fallback POL_USD_ASSUMED.
- connect проверяет chain_id == 137 (WRONG_CHAIN): чужая сеть с Polygon-адресом
  Pool даёт молчаливый 100% data_unavailable.
- logger: имена файлов с uuid-суффиксом — два прохода в одну секунду не делят
  JSONL (append дал бы дубли по user).
- RPC_DOWN посреди опроса вотч-листа: курсор covered discovery и вотч-лист
  сохраняются, обнаруженные юзеры не теряются.
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

Развёрнутая реализация Pool (proxy 0x794a...14aD) = 0x9a375fa209ebb87832eb258bfae02998bf3641aa,
Sourcify full match, исходники из aave-v3-origin. НЕ сверяться с aave-v3-core
master: там старая логика closeFactor без $2000-порога и без MIN_LEFTOVER_BASE.
RPC: с сервера работает polygon.drpc.org (30/30 на 10 rps), 1rpc.io нестабилен
(Cloudflare/лимиты); с ноутбука — polygon.gateway.tenderly.co; blastapi/llamarpc мертвы.

## Спеки

Синхронизировать с кодом: сигнатура net_profit(debt_usd, bonus_pct, fee_pct, gas_usd, swap_cost_pct=0.0),
parse_account len==6, UiPoolDataProvider вместо getUserConfigurationMap,
батч 200 адаптивный, top_position -> 5-tuple (с eMode-категорией),
executable_cover_usd -> (cover_usd, limit). Проверять grep'ом перед каждой правкой спеки.
Тесты: python -m pytest tests/ -q (71 шт: 23 calc, 15 reserves, 15 scan, 6 discovery, 9 rpc_client, 3 parser).
