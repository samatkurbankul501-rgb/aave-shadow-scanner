# План реализации: Shadow-сканер ликвидаций Aave V3 (этап B)

Дата: 2026-11-04 (синхронизировано с кодом 2026-11-10)
Спека: docs/superpowers/specs/2026-11-04-liquidator-scanner-design.md
Маршрут: B (этот план) → C (testnet) → A (mainnet боевой контур). Не входит: контракт Solidity, flash loan, приватные ключи.

## Конвенция и структура

- Stack: Python 3.11, только web3.py (+ pytest для тестов). Без async/фреймворков.
- Корень проекта: C:/Users/User/Downloads/nn/ (git-репо; документы в docs/, код в scanner/, деплой в deploy/).
- Файлы: scanner/rpc_client.py, scanner/discovery.py, scanner/calc.py, scanner/parser.py, scanner/reserves.py, scanner/logger.py, scanner/scan.py (точка входа), tests/test_calc.py, tests/test_parser.py, tests/test_scan.py, tests/test_reserves.py, tests/test_discovery.py, tests/fixtures/two_good_bad.json.
- Имя переменных и интерфейсов — из спеки: connect(url)->Web3, rpc_call(w3, fn, *args), scan_range(w3, from_block, to_block)->(set, last_ok), net_profit(debt_usd, bonus_pct, flash_fee_pct, gas_usd)->float_usd, log_entry(dict), summary(dict).
- Форматы логов из спеки: JSONL {ts, user, hf, debt_usd, collateral_usd, net_profit_usd, block, block_hash}; сводный JSON {pass_start_ts, pass_end_ts, candidates_checked, liquidatable, net_positive, sum_net_profit_usd, candidates}.
- Баунды из спеки: порог долга $50, HF < 1.0, confirmations 16, вотч-лист лимит 5000, батч 200 блоков адаптивный (сжатие пополам до MIN_BATCH=5), rate limit ~10 req/s, backoff Retry-After минимум 5с, data_unavailable < 5%.

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
1. Реализовать connect(url): Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10})), проверка w3.is_connected() → иначе raise SystemExit('RPC_DOWN'); проверка chain_id == 137 → иначе SystemExit('WRONG_CHAIN').
2. Реализовать rpc_call(w3, fn, *args): try/except; при 429 — backoff по Retry-After (минимум 5с), инкремент rl_hits; 3 подряд → raise SystemExit('RPC_DOWN'); при timeout — 1 ретрай 2с. Throttle минимум 100ms между вызовами.
3. Реализовать get_block_number(w3) через свойство w3.eth.block_number (метода get_block_number у web3.py нет).
4. Тест ручной: python -c "from scanner.rpc_client import connect; print(connect('https://polygon-bor-mainnet.public.blastapi.io').eth.block_number)" — должен печатать номер блока.
Проверка: ручной вызов печатает блок (не RPC_DOWN).

## Задача 3. discovery.py (скан логов Pool)

Файлы: scanner/discovery.py
Цель: scan_range(from_block, to_block) -> set[address] из событий Borrow+Supply.
Шаги:
1. Константы: POOL_POLYGON (POOL_AMOY=None — Aave V3 на Amoy не задеплоен), ABI с полными сигнатурами событий Borrow/Supply (onBehalfOf indexed).
2. scan_range: батчит по 200 блоков (адаптивное сжатие пополам до MIN_BATCH=5 при ошибке RPC), eth_getLogs напрямую по темам Borrow и Supply, адрес позиции = onBehalfOf из topics[2]; при ошибке eth_getLogs посреди диапазона — возвращает частичный результат и курсор covered_end (фактически покрытый конец, следующий проход продолжит с него +1).
3. Тест: python -m scanner.discovery --from <N> --to <N+20> на реальном RPC print len(set).
Проверка: print неотрицательное число адресов, без исключений.

## Задача 4. logger.py (JSONL + сводный JSON)

Файлы: scanner/logger.py
Цель: log_entry(dict) + summary(dict) с дедупликацией.
Шаги:
1. log_entry: дедупликация по user — одна запись на проход, при дубле свежая запись перезаписывает все поля (hf/ts/net_profit_usd), JSONL-файл перезаписывается.
2. summary: пишет pass JSON {pass_start_ts, pass_end_ts, candidates_checked, liquidatable, net_positive, sum_net_profit_usd, candidates}, возвращает путь файла.
3. Тест: python -c "from scanner.logger import log_entry, summary; r1=log_entry({'user':'0x1','hf':0.97,'ts':1}); r2=log_entry({'user':'0x1','hf':0.98,'ts':2}); print(summary({'candidates_checked':1,'liquidatable':1,'net_positive':1,'sum_net_profit_usd':9.89,'candidates':[r2]}))" — r1 и r2 не одновременно в candidates, кандидаты содержат один адрес.
Проверка: вывод summary_path, JSON файл существует.

## Задача 5. scan.py (точка входа, полный проход)

Файлы: scanner/scan.py
Цель: CLI-проход из спеки (поток 30-60 сек при малом вотч-листе; при 5000 адресов и throttle 100 мс — до ~13 мин).
Шаги:
1. argparse: --rpc (обязательный), --from_block (int, дефолт = текущий - 10000), --out (папка логов, дефолт logs).
2. Связать компоненты: connect → scan_range → getUserAccountData по вотч-листу (лимит 5000, детерминированный срез sorted(users)[:5000]; долг неизвестен до опроса, вытеснение бездолговых невозможно) → calc.net_profit (fee = 0.05% фикс для оценки; closeFactor по контракту calc.coverable_debt: 100% по умолчанию, cap 50% общего долга только при HF>0.95 и обоих резервах >=$2000; debtToCover с топ-актива через borrowIndex; gas по eth_estimateGas от реального ликвидатора с balanceOf+allowance на реальных liquidationCall-аргументах; revert/нет ликвидатора → gas_assumed + net по fallback-газу (GAS_EST_DEFAULT); чтения пиннятся к last_ok) → logger.log_entry/summary. Курсор персистится в logs/cursor.txt и продолжается всегда (resume_from_block), явный --from_block побеждает.
3. Сухой запуск: python -m scanner.scan --rpc <RPC> --from_block <N> --out logs_smoke → должен завершить проход, записать JSONL + сводный JSON.
4. Проверка реорга: передавать block_number - 16 как confirmations (глубина 16).
Проверка: сводный JSON напечатан, JSONL файл существует с 0..N строками.

## Задача 6. tests/test_parser.py + фикстуры

Файлы: tests/test_parser.py, tests/fixtures/two_good_bad.json
Цель: парсер getUserAccountData с ассертами.
Шаги:
1. Сохранить два JSON в tests/fixtures/two_good_bad.json: good (hf 1.12, user 0xabc…, collateral_usd 184.50, debt_usd 50.0, len 6) и bad (обрезанный JSON).
2. Написать test_parser.py: good → assert hf==1.12, collateral_usd==184.5, debt_usd==50.0, len==6; bad → assert pytest.raises(ValueError).
3. Запустить pytest tests/test_parser.py -q → PASS.
Проверка: pytest -q (2 passed).

## Задача 7. Smoke dry-run на тестнете (Sepolia или свой деплой)

Цель: 1 час-на-тестсети smoke из спеки (до 2-4 недель на Polygon).
Aave V3 на Amoy не задеплоен (нет AaveV3Amoy.ts в aave-address-book, POOL_AMOY=None) — Amoy-вариант отменён; Polygon-адрес на Amoy молча даст пустоту.
Шаги:
1. Взять Sepolia-деплой Aave V3 (или свой деплой) и подставить его адрес POOL вместо Polygon (сейчас POOL = POOL_POLYGON захардкожен в scan.py).
2. Запустить 1 час: python -m scanner.scan --rpc <SEPOLIA_RPC> --from_block <cur-1000> --out logs_sepolia (прервать через 1 ч Ctrl+C).
3. Ассерты dry-run: нет отправок транзакций (ключей нет, только read-only вызовы), нет дубликатов по user в JSONL, data_unavailable < 5%, реорг-блоков нет (confirmations 16).
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
