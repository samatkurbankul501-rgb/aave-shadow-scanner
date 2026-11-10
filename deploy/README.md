# Deploy: Aave V3 Shadow Scanner -> Ubuntu

## 0. Подключение (сейчас 10.60.100.33 недоступен)

IP из таблицы — приватный 10.x, не маршрутизируется из интернета.
Выбрать один из вариантов:

### Вариант A: Tailscale (самый лёгкий, 3 команды)

На сервере (нужен доступ к консоли/VNC):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

На ноутбуке: `tailscale up` и `tailscale status`
Подключение: `ssh wsuk-ubuntu@100.x.x.x`

### Вариант B: Публичный IP / NAT port-forward

Попросить админа дать внешний IP и прокинуть:
`публичный-IP:2222 -> 10.60.100.33:22`

### Вариант C: доступ в той же подсети

Серверу дать IP из твоей сети 10.201.217.0/24.

## 1. Развёртка

```bash
chmod +x deploy/setup.sh
REPO_URL=<url_твоего_репо> ./deploy/setup.sh
```

Затем smoke вручную:

```bash
/opt/aave-scanner/.venv/bin/python -m scanner.scan --rpc https://polygon-bor-mainnet.public.blastapi.io --from_block <cur-1_200_000> --out logs  # ~2 недели истории; 55M блоков = ~2.6 года
```

## 2. Запуск как сервис

```bash
sudo systemctl start aave-scanner
journalctl -u aave-scanner -f
```

## 3. Проверки smoke

- `logs/pass_*.json` создаётся, поля `candidates_checked, liquidatable, net_positive`
- `logs/candidates_*.jsonl` — строки с `net_profit_usd` числом (не None)
- `data_unavailable < 5%` от `candidates_checked`
- `gas_assumed` в pass JSON: доля fallback-газа; если >50% — estimateGas от нейтрального `from: ZERO_ADDRESS` массово ревертит (у нулевого адреса нет токенов на repayment), оценка газа не реалистична (этап C пересмотрит)
- Нет дубликатов по user в JSONL (дедуп в logger, свежий HF в файле)
- Нет роста nonce — транзакции не шлются, ключей нет

## 4. Безопасность

- RPC ключ только в `/opt/aave-scanner/.env` (chmod 600), не в git
- Приватных ключей на сервере НЕТ. Stage B только чтение.
- SSH только по ключу, PasswordAuthentication no, ufw только 22
- logrotate под logs/

## 5. Гейт B -> C

Вести 14 дней. Переход только если:
всего >=5 кандидатов с net_profit_usd > $3 И >=1 с > $10.
Иначе протокол не подходит — идти на Venus/Moonwell тем же кодом.
