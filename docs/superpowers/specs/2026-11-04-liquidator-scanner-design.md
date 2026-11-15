# Shadow-сканер ликвидаций Aave V3 (этап B)

Дата: 2026-11-04
Статус: реализовано (этап B); синхронизировано с кодом 2026-11-15 (71 тест)
Маршрут: B (shadow-сканер) → C (testnet) → A (mainnet боевой контур). Этот документ описывает ТОЛЬКО этап B.

## Цель

Ответить деньгами-честно на вопрос: «Есть ли на Aave V3 Polygon позиции с HF < 1, где чистый плюс после flash fee и газа > порога?» — за 2-4 недели истории без единой транзакции и без приватного ключа.

## Архитектура (4 компонента)

1. **rpc_client** — подключение к RPC Polygon (Alchemy/QuickNode/BlockPi, бесплатный тир). Интерфейс: connect(url) -> Web3 (проверяет chain_id == 137, иначе WRONG_CHAIN), rpc_call(w3, fn, *args) -> result, get_block_number(w3) -> int. Зависит только от web3.py. Без приватных ключей.
2. **discovery** — курсор по блокам, фильтрует логи Pool Aave V3 (Borrow, Supply). Интерфейс: scan_range(w3, from_block, to_block) -> (set[address], last_ok_block). Адрес позиции — onBehalfOf из topics[2] (user в событиях не indexed). Выход — множество юзеров с долгами. Не зависит от калькулятора.
3. **calc** — фильтр и оценка: HF < 1.0 + долг >= $50 → net_profit = коллатерал после swap - flash-погашение - gas. Интерфейс: net_profit(debt_usd, bonus_pct, flash_fee_pct, gas_usd, swap_cost_pct=0.0) -> float_usd; net_liquidation_bonus(gross_bonus_pct, protocol_fee_bps) -> net % (fee срезает часть бонус-коллатерала по LiquidationLogic); executable_cover_usd(hf, reserve_debt_usd, reserve_coll_usd, total_debt_usd, gross_bonus_pct) -> (cover_usd, limit) — исполнимое покрытие с учётом closeFactor cap, достаточности коллатерала и анти-пылевого правила MIN_LEFTOVER_BASE=$1000 (limit: None|collateral|dust). Чистые функции, тестируемы без RPC.
4. **logger** — пишет JSONL-лог и сводный JSON по проходу. Интерфейс: log_entry(candidate_dict), summary(pass_dict).

## Что делает B

Подключение → discovery → опрос вотч-листа (getUserAccountData) → фильтр → кальция → logger.

Вход: RPC URL (строка) + from_block (int, дефолт = текущий блок - 10000) + порог долга $50. Персистентный курсор (logs/cursor.txt) продолжается всегда, даже если отстал больше чем на 10000 блоков (без пропуска истории после простоя); явный --from_block побеждает курсор. Вотч-лист персистится между проходами (logs/watchlist.txt, атомарная запись): discovery даёт только новых юзеров окна, персистентные перепроверяются каждый проход (HF дрейфует ниже 1 без новых Borrow/Supply); бездолговые (debt_usd==0) вычищаются после опроса, data_unavailable остаются; при RPC_DOWN вотч-лист сохраняется вместе с курсором. Список токенов не нужен: discovery выводит юзеров из событий Borrow/Supply.
Выход: JSONL-лог (по строке на кандидата) + сводный JSON по проходу. Зависимости: только web3.py. Никаких ключей.

Формат JSONL-строки (units):
`{ts: int_unix, user: hex_addr, hf: float, debt_usd: float, collateral_usd: float, net_profit_usd: float, block: int, block_hash: hex}`
Сводный JSON по проходу: `{pass_start_ts: int, pass_end_ts: int, candidates_checked: int, liquidatable: int, net_positive: int, sum_net_profit_usd: float, candidates: [...]}` (код добавляет data_unavailable, gas_assumed, rl_hits, last_ok_block, watchlist_truncated, watchlist_carried, watchlist_persisted, reserve_blocked, emode_unreadable, reserve_cache_size, pol_price_usd, pol_price_source, swap_cost_pct_assumed, liq_fee_unreadable). JSONL-строка кода пишет также debt_asset, debt_to_cover_usd, bonus_pct, emode_category, swap_cost_pct, liquidation_protocol_fee_bps, gas_usd, cover_limit (collateral|dust) при урезании покрытия, emode_bonus_unreadable при непрочитанной eMode-категории и flag=gas_assumed при fallback-газе.

## Поток за проход (30-60 сек при малом вотч-листе; при 5000 адресов и throttle 100 мс — до ~13 мин)

1. RPC → blockNumber. Не отвечает → стоп с ошибкой RPC_DOWN после 1 ретрая с backoff 2с.
2. Discovery: диапазон from_block..blockNumber, события Borrow+Supply из Pool, батч 200 блоков с адаптивным уменьшением пополам при ошибке RPC (в пределах rate limit ~10 req/s бесплатного RPC, курсор продолжается).
3. Для каждого юзера вотч-листа (персистентный ∪ новые из discovery) — getUserAccountData.
4. Опрос вотч-листа: актуальный HF каждого.
5. Фильтр HF < 1.0 + долг >= $50.
6. Оценка: UiPoolDataProvider.getUserReservesData → реальный debtAsset + топ-коллатерал, ранжирование по USD через кэш резервов (без кэша — legacy по scaled) + eMode-категория юзера; inactive/paused резервы (кэш) и grace-период (getLiquidationGracePeriod > timestamp блока) блокируют кандидата (reserve_blocked); бонус — из КОЛЛАТЕРАЛЬНОГО резерва (LiquidationLogic), NET после liquidationProtocolFee (биты 152-167 getConfiguration; net = gross×(1−fee_bps/10000), fee не прочитан → gross + liq_fee_unreadable в сводном); при eMode != 0 и коллатерале в collateralBitmap категории — бонус КАТЕГОРИИ (getEModeCategoryData, gross; fee остаётся от резерва; не прочитана → резервный бонус + emode_unreadable); покрытие долга — calc.executable_cover_usd: closeFactor по контракту (100% долга резерва по умолчанию; cap = 50% ОБЩЕГО долга только при HF строго > 0.95 и обоих резервах >= $2000; коллатерал резерва в USD из scaledATokenBalance × liquidityIndex // RAY) + достаточность коллатерала (collateral/(1+gross) — контракт урезает долг и изымает весь коллатерал) + анти-пылевое правило MIN_LEFTOVER_BASE=$1000 (частичная ликвидация оставляет >= $1000 и долга, и коллатерала, иначе MustNotLeaveDust; запрос урезается до исполнимого, cover_limit в JSONL); debtToCover = scaledDebt × variableBorrowIndex // RAY с топ-одного актива, USD через per-reserve цену (агрегат — fallback); gas через eth_estimateGas на call (без отправки, from — реальный ликвидатор из свежих LiquidationCall-логов с проверенными balanceOf + allowance). Все чтения пиннятся к last_ok (block_identifier) — block/block_hash в логе соответствует снятым данным. Чистый плюс = debt×(1+bonus/100)×(1−swap_cost/100) − debt×(1+flashFee/100) − gas; swap_cost — допущение этапа B (SWAP_COST_PCT_ASSUMED=0.3, комиссия DEX; slippage не моделируется, этап C — живые котировки). Только статика, только лог.

## Обработка ошибок

- Чужая сеть: connect проверяет chain_id == 137 → SystemExit WRONG_CHAIN (иначе Polygon-адрес Pool в чужой сети = молчаливый 100% data_unavailable).
- Цена газа: gas_usd = gas_est × gas_price_wei × 1e-18 × цена POL (wei→POL); цена POL из оракула Aave (networkBaseTokenPriceInUsd), fallback POL_USD_ASSUMED; в сводном JSON pol_price_usd/pol_price_source.
- RPC_DOWN посреди опроса вотч-листа: курсор покрытой discovery и вотч-лист сохраняются, следующий проход не переделывает скан логов и не теряет обнаруженных юзеров.

- RPC таймаут/не отвечает: 1 ретрай backoff 2с, затем стоп RPC_DOWN.
- 429 / rate limit: backoff по Retry-After (минимум 5с), счётчик rl_hits в сводном JSON; 3 подряд → стоп RPC_DOWN.
- eth_getLogs вернул ошибку на середине discovery: достигнут max блок диапазона, курсор продолжается в следующем проходе с последнего успешно полученного блока.
- eth_estimateGas revert или не найден ликвидатор с балансом/approve: помечаем кандидата gas_assumed, net_profit считается по fallback-газу (GAS_EST_DEFAULT) — иначе гейт B->C всегда 0.
- Ошибка опроса вотч-листа (revert getUserAccountData): пропускаем юзера, помечаем data_unavailable, продолжаем опрос остальных.
- Вотч-лист растёт без лимита: лимит 5000 адресов; при переполнении — детерминированный срез sorted(users)[:5000]. Бездолговые (debt_usd==0) вычищаются из персистентного вотч-листа после опроса; юзеры с data_unavailable остаются (долг неизвестен).
- Резерв кандидата inactive/paused (getReservesData) или в grace-периоде (getLiquidationGracePeriod > timestamp блока): ликвидация ревертит (ValidationLogic) — кандидат пропускается, счётчик reserve_blocked в сводном.
- eMode-категория не прочитана (revert getEModeCategoryData/bitmap): fallback на резервный бонус, emode_bonus_unreadable в JSONL + emode_unreadable в сводном.
- getUserAccountData ревертит (кандидат исчез/не читаем): пропуск, счётчик data_unavailable, опрост остальных продолжаем.
- Оракул = 0 (без фида): per-reserve цена не подставляется в формулу (долг — агрегат USD из getUserAccountData); бонус берётся per-reserve из кэша (net после protocol fee), при пустом кэше — flat 5%. Кэш пуст при decode-fail getReservesData: UIDP_ABI обязан совпадать с деплоем (40 полей AggregatedReserveData, сверено Sourcify; регрессия test_uidp_abi_matches_deployed_layout), reserve_cache_size=0 виден в сводном.
- Реорг: сканируем только подтверждённые блоки (глубина 16), идемпотентность по хэшу блока.
- Дубликаты: дедупликация по адресу, свежая запись перезаписывает все поля (hf/ts/net_profit_usd), в JSONL ровно одна строка на user.

## Тестирование (без реальных денег)

1. Unit-тест калькулятора — детерминированные входы: HF 0.97, долг $200, бонус 5%, fee = 0.05% (фикс), gas = $0.01. Ожидаемый net = $9.89. Любая правка формулы роняет тест.
2. Фикстурный тест парсера — два сохранённых JSON: нормальный getUserAccountData → assert hf==1.12, collateral_usd==184.5, debt_usd==50.0, len==6; кривой JSON → assert бросает ValueError.
3. Dry-run в 2 этапа: smoke 1 час на тестсети (Sepolia или свой деплой: Aave V3 на Amoy не задеплоен, POOL_AMOY=None) с ассертами: нет отправок транзакций, нет дубликатов по user в JSONL, реорг-блоков нет (глубина 16), пропуски опросов (data_unavailable) < 5%. На Polygon — 2-4 недели истории (цель, не 1 час).

## Критерий выхода из B

Числовой порог перехода: вести 2 недели истории (мин. 14 дней). Переход к C только если: (а) минимум 5 кандидатов с net_profit_usd > $3 за всё время И (б) минимум 1 кандидат с net_profit > $10. Иначе — протокол не подходит, переходим к Venus/Moonwell тем же кодом. Сканер НЕ даёт вердикт «есть деньги» — но с этим порогом он даёт проверяемый вердикт «идём дальше или нет».

## Вне скоупа B

- Smart-контракт / flash loan / дефолт Solidity (это этап A)
- Testnet-дефолт (это этап C)
- Реальный ввод приватного ключа

## Почему не сразу A/C

Код из исходного PDF — псевдокод: executeOperation без вызова liquidate(), неполные интерфейсы Aave V3 (amount_t, flashLoanSimple с неверными сигнатурами), пустой ABI, захардкоженный юзер. Деплой такого на mainnet = сжигание газа. B сначала доказывает, что окна существуют.
