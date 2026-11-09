# План реализации: Shadow-сканер ликвидаций Aave V3 (этап B)

Дата: 2026-11-04
Спека: docs/superpowers/specs/2026-11-04-liquidator-scanner-design.md
Маршрут: B (этот план) → C (testnet) → A (mainnet боевой контур). Не входит: контракт Solidity, flash loan, приватные ключи.

## Конвенция и структура

- Stack: Python 3.11, только web3.py (+ pytest для тестов). Без async/фреймворков.
- Корень проекта: C:/Users/User/Downloads/nn/ (не git репо — документы в docs/, код в scanner/).
- Файлы: scanner/rpc_client.py, scanner/discovery.py, scanner/calc.py, scanner/logger.py, scanner/scan.py (точка входа), tests/test_calc.py, tests/test_parser.py, tests/fixtures/two_good_bad.json.
- Имя переменных и интерфейсов — из спеки: connect(url)->Web3, scan_range(from_block, to_block), net_profit(candidate, reserve_data, gas_est)->float_usd, log_entry(dict), summary(dict).
- Форматы логов из спеки: JSONL {ts, user, hf, debt_usd, collateral_usd, net_profit_usd, block, block_hash}; сводный JSON {pass_start_ts, pass_end_ts, candidates_checked, liquidatable, net_positive, sum_net_profit_usd, candidates}.
- Баунды из спеки: порог долга $50, HF < 1.0, confirmations 16, вотч-лист лимит 5000, батч 10-20 блоков, rate limit ~10 req/s, backoff Retry-After минимум 5с, data_unavailable < 5%.

---

## Задача 1. calc.py + test_calc.py (детерминированная формула)

Файлы: scanner/calc.py, tests/test_calc.py
Цель: чистая функция net_profit без RPC.
Шаги:
1. Создать scanner/__init__.py (пустой) и scanner/calc.py с функцией net_profit(debt_usd: float, bonus_pct: float, flash_fee_pct: float, gas_usd: float) -> float_usd.
2. Написать tests/test_calc.py с 3 тестами: (a) спечная цифра $9.89 (долг $200, бонус 5%, fee 0.05%, gas $0.01); (b) net = 0 при bonus ровно = fee+gas; (c) net отрицателен когда bonus < fee. Ожидаемый net = debt*bonus - debt*fee - gas.
3. Запустить pytest tests/test_calc.py -q → все PASS.
Проверка: pytest -q (3 passed).

## Задача 2. rpc_client.py (подключение + rate limit + backoff)

Файлы: scanner/rpc_client.py
Цель: connect(url)->Web3 + rpc_call с backoff.
Шаги:
1. Реализовать connect(url): Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10})), проверка w3.is_connected() → иначе raise SystemExit('RPC_DOWN').
2. Реализовать rpc_call(fn, *args): try/except; при 429 — backoff по Retry-After (минимум 5с), инкремент rl_hits; 3 подряд → raise SystemExit('RPC_DOWN'); при timeout — 1 ретрай 2с.
3. Реализовать get_block_number() через rpc_call(w3.eth.block_number).
4. Тест ручной: python -c "from scanner.rpc_client import connect; print(connect('https://polygon-bor-mainnet.public.blastapi.io').eth.block_number)" — должен печатать номер блока.
Проверка: ручной вызов печатает блок (не RPC_DOWN).

## Задача 3. discovery.py (скан логов Pool)

Файлы: scanner/discovery.py
Цель: scan_range(from_block, to_block) -> set[address] из событий Borrow+Supply.
Шаги:
1. Константы: POOL_POLYGON, AAVE_V3_POOL, ABI минимальная (Borrow, Supply события с полями user, amount).
2. scan_range: батчит по 20 блоков, eth_getLogs topics Borrow|Supply, фильтрует user, при ошибке eth_getLogs посреди диапазона — возвращает частичный результат и выставляет частичный курсор (то есть следующий проход продолжит с последнего успешного блока).
3. Тест: python -m scanner.discovery --from <N> --to <N+20> на реальном RPC print len(set).
Проверка: print неотрицательное число адресов, без исключений.

## Задача 4. logger.py (JSONL + сводный JSON)

Файлы: scanner/logger.py
Цель: log_entry(dict) + summary(dict) с дедупликацией.
Шаги:
1. log_entry: дедупликация по user — одна запись на проход, при дубле обновляется hf и ts.
2. summary: пишет pass JSON {pass_start_ts, pass_end_ts, candidates_checked, liquidatable, net_positive, sum_net_profit_usd, candidates}, возвращает путь файла.
3. Тест: python -c "from scanner.logger import log_entry, summary; r1=log_entry({'user':'0x1','hf':0.97,'ts':1}); r2=log_entry({'user':'0x1','hf':0.98,'ts':2}); print(summary({'candidates_checked':1,'liquidatable':1,'net_positive':1,'sum_net_profit_usd':9.89,'candidates':[r2]}))" — r1 и r2 не одновременно в candidates, кандидаты содержат один адрес.
Проверка: вывод summary_path, JSON файл существует.

## Задача 5. scan.py (точка входа, полный проход)

Файлы: scanner/scan.py
Цель: CLI-проход из спеки (поток 30-60 сек при малом вотч-листе; при 5000 адресов и throttle 100 мс — до ~13 мин).
Шаги:
1. argparse: --rpc (обязательный), --from_block (int, дефолт = текущий - 10000), --out (папка логов, дефолт logs).
2. Связать компоненты: connect → scan_range → getUserAccountData по вотч-листу (лимит 5000, вытеснение бездолговых) → calc.net_profit (fee = 0.05% фикс для оценки, gas по eth_estimateGas/staticcall; revert → gas_assumed + net по fallback-газу (GAS_EST_DEFAULT)) → logger.log_entry/summary.
3. Сухой запуск: python -m scanner.scan --rpc <RPC> --from_block <N> --out logs_smoke → должен завершить проход, записать JSONL + сводный JSON.
4. Проверка реорга: передавать block_number - 16 как confirmations (глубина 16).
Проверка: сводный JSON напечатан, JSONL файл существует с 0..N строками.

## Задача 6. tests/test_parser.py + фикстуры

Файлы: tests/test_parser.py, tests/fixtures/two_good_bad.json
Цель: парсер getUserAccountData с ассертами.
Шаги:
1. Сохранить два JSON в tests/fixtures/two_good_bad.json: good (hf 1.12 abc user, debt_usd 184.50, len 6) и bad (обрезанный JSON).
2. Написать test_parser.py: good → assert hf==1.12, debt_usd==184.5, len==6; bad → assert pytest.raises(ValueError).
3. Запустить pytest tests/test_parser.py -q → PASS.
Проверка: pytest -q (2 passed).

## Задача 7. Smoke dry-run на Amoy (полигон тестнет)

Цель: 1 час-на-тестсети smoke из спеки (до 2-4 недель на Polygon).
Шаги:
1. Сменить RPC на Amoy в scanner/scan.py --rpc <Amoy RPC> (константа POOL = Amoy Aave V3).
2. Запустить 1 час: python -m scanner.scan --rpc <AMOY_RPC> --from_block <cur-1000> --out logs_amoy (прервать через 1 ч Ctrl+C).
3. Ассерты dry-run: нет отправок транзакций (проверить nonce не вырос — хэш контракта не изменён), нет дубликатов по user в JSONL, data_unavailable < 5%, реорг-блоков нет.
Проверка: PASS ассертов, вопросы пользователю.

## Контрольный список после всех задач

- pytest -q → все passed
- python -m scanner.scan --rpc <POLYGON_RPC> --from_block <N> → JSONL + сводный JSON
- Оценка: 4 минимальных вызова Python (подключение RPC, скан логов, оценка gas, запись) должны падать-managed (RL/ноль данных), без паники и без транзакций

## Что в этом плане НЕ делать

- Не делать Solidity-контракт / flash loan / дефолт (это этап A)
- Не деплоить на Amoy/Polygon без явного апрува пользователя
- Не вводить приватные ключи
- Не добавлять async, фреймворки, конфиг-файлы сверх argparse
