# Shadow-сканер ликвидаций Aave V3 (этап B)

Дата: 2026-11-04
Статус: дизайн на ревью (итерация 2, после ревью)
Маршрут: B (shadow-сканер) → C (testnet) → A (mainnet боевой контур). Этот документ описывает ТОЛЬКО этап B.

## Цель

Ответить деньгами-честно на вопрос: «Есть ли на Aave V3 Polygon позиции с HF < 1, где чистый плюс после flash fee и газа > порога?» — за 2-4 недели истории без единой транзакции и без приватного ключа.

## Архитектура (4 компонента)

1. **rpc_client** — подключение к RPC Polygon (Alchemy/QuickNode/BlockPi, бесплатный тир). Интерфейс: connect(url) -> Web3, rpc_call(fn, args) -> result, get_block_number() -> int. Зависит только от web3.py. Без приватных ключей.
2. **discovery** — курсор по блокам, фильтрует логи Pool Aave V3 (Borrow, Supply). Интерфейс: scan_range(from_block, to_block) -> set[address]. Выход — множество юзеров с долгами. Не зависит от калькулятора.
3. **calc** — фильтр и оценка: HF < 1.0 + долг >= $50 → net_profit = bonus - flashFee - gas. Интерфейс: net_profit(candidate, reserve_data, gas_est) -> float_usd. Чистая функция, тестируема без RPC.
4. **logger** — пишет JSONL-лог и сводный JSON по проходу. Интерфейс: log_entry(candidate_dict), summary(pass_dict).

## Что делает B

Подключение → discovery → опрос вотч-листа (getUserAccountData) → фильтр → кальция → logger.

Вход: RPC URL (строка) + список токенов Aave V3 Polygon [addr...] + from_block (int, дефолт = текущий блок - 10000) + порог долга $50.
Выход: JSONL-лог (по строке на кандидата) + сводный JSON по проходу. Зависимости: только web3.py. Никаких ключей.

Формат JSONL-строки (units):
`{ts: int_unix, user: hex_addr, hf: float, debt_usd: float, collateral_usd: float, net_profit_usd: float, block: int, block_hash: hex}`
Сводный JSON по проходу: `{pass_start_ts: int, pass_end_ts: int, candidates_checked: int, liquidatable: int, net_positive: int, sum_net_profit_usd: float, candidates: [...]}`.

## Поток за проход (30-60 сек)

1. RPC → blockNumber. Не отвечает → стоп с ошибкой RPC_DOWN после 1 ретрая с backoff 2с.
2. Discovery: диапазон from_block..blockNumber, события Borrow+Supply из Pool, батчи по 10-20 блоков на вызов (в пределах rate limit ~10 req/s бесплатного RPC, курсор продолжается).
3. Для каждого нового юзера — getUserAccountData → вотч-лист.
4. Опрос вотч-листа: актуальный HF каждого.
5. Фильтр HF < 1.0 + долг >= $50.
6. Оценка: getUserConfigurationMap + getReservesData → бонус, closeFactor (обычно 50%), gas через eth_estimateGas на call (без отправки). Чистый плюс = бонус - flashFee - gas. Только статика, только лог.

## Обработка ошибок

- RPC таймаут/не отвечает: 1 ретрай backoff 2с, затем стоп RPC_DOWN.
- 429 / rate limit: backoff по Retry-After (минимум 5с), счётчик rl_hits в сводном JSON; 3 подряд → стоп RPC_DOWN.
- eth_getLogs вернул ошибку на середине discovery: достигнут max блок диапазона, курсор продолжается в следующем проходе с последнего успешно полученного блока.
- eth_estimateGas revert: помечаем кандидата gas_unestimable, net_profit не вычисляем (null).
- Ошибка опроса вотч-листа (revert getUserAccountData): пропускаем юзера, помечаем data_unavailable, продолжаем опрос остальных.
- Вотч-лист растёт без лимита: лимит 5000 адресов, при переполнении вытесняются адреса без долга.
- Кандидат исчез при чтении: пропуск, метка already_liquidated.
- Оракул = 0 (без фида): метка no_oracle, не считаем.
- Реорг: сканируем только подтверждённые блоки (глубина 16), идемпотентность по хэшу блока.
- Дубликаты: дедупликация по адресу, оставляем свежий HF.

## Тестирование (без реальных денег)

1. Unit-тест калькулятора — детерминированные входы: HF 0.97, долг $200, бонус 5%, fee = 0.05% (фикс), gas = $0.01. Ожидаемый net = $9.89. Любая правка формулы роняет тест.
2. Фикстурный тест парсера — два сохранённых JSON: нормальный getUserAccountData → assert hf==1.12, debt_usd==184.5, len==12; кривой JSON → assert бросает ValueError.
3. Dry-run в 2 этапа: smoke 1 час на тестсети (Amoy) с ассертами: нет отправок транзакций, нет дубликатов по user в JSONL, реорг-блоков нет (глубина 16), пропуски опросов (data_unavailable) < 5%. На Polygon — 2-4 недели истории (цель, не 1 час).

## Критерий выхода из B

Числовой порог перехода: вести 2 недели истории (мин. 14 дней). Переход к C только если: (а) минимум 5 кандидатов с net_profit_usd > $3 за всё время И (б) минимум 1 кандидат с net_profit > $10. Иначе — протокол не подходит, переходим к Venus/Moonwell тем же кодом. Сканер НЕ даёт вердикт «есть деньги» — но с этим порогом он даёт проверяемый вердикт «идём дальше или нет».

## Вне скоупа B

- Smart-контракт / flash loan / дефолт Solidity (это этап A)
- Testnet-дефолт (это этап C)
- Реальный ввод приватного ключа

## Почему не сразу A/C

Код из исходного PDF — псевдокод: executeOperation без вызова liquidate(), неполные интерфейсы Aave V3 (amount_t, flashLoanSimple с неверными сигнатурами), пустой ABI, захардкоженный юзер. Деплой такого на mainnet = сжигание газа. B сначала доказывает, что окна существуют.
