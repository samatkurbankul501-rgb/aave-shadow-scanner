# Deploy: Aave V3 Shadow Scanner -> Ubuntu (22.04/24.04/26.04)

setup.sh берёт python3.11 из apt, если пакет есть (22.04/24.04), иначе системный
python3 с проверкой >= 3.11 (26.04). Сервис работает под тем же пользователем,
от которого запущен setup (владелец /opt/aave-scanner и User= unit совпадают).

## 0. Подключение (dell-r740 доступен через Tailscale)

Проверено 2026-11-15: `ssh dell-r740` работает с ноутбука (Tailscale 100.86.76.38,
беспарольно). Приватный 10.60.100.33 из интернета не маршрутизируется — не использовать.
Если Tailscale отвалится, запасные варианты:

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

Затем smoke вручную (RPC: polygon.drpc.org проверен с сервера 30/30 на 10 rps;
1rpc.io нестабилен — Cloudflare/лимиты; blastapi/llamarpc мертвы):

```bash
/opt/aave-scanner/.venv/bin/python -m scanner.scan --rpc https://polygon.drpc.org --from_block <cur-1_200_000> --out logs  # ~2 недели истории; 55M блоков = ~2.6 года
```

## 2. Запуск как сервис

```bash
sudo systemctl start aave-scanner
journalctl -u aave-scanner -f
```

## 3. Проверки smoke

- `logs/pass_*.json` создаётся, поля `candidates_checked, liquidatable, net_positive`
- `reserve_cache_size > 0`: если 0 — ABI UiPoolDataProvider не совпал с деплоем
  (decode fail) или RPC недоступен; бонус плоский 5%, проверить живым вызовом
- `pol_price_source: oracle` — цена POL из оракула Aave; `assumed` — fallback-допущение
- `swap_cost_pct_assumed` — допущение издержек swap коллатерала обратно в debt-актив (этап B, комиссия DEX); slippage не моделируется — этап C заменит живыми котировками
- `liq_fee_unreadable == 0` — protocol fee прочитан по всем резервам; если >0, бонус этих резервов считается gross (завышен), net_profit оптимистичен
- `logs/watchlist.txt` создан; `watchlist_persisted` — сколько юзеров перепроверяется в следующем проходе; `watchlist_carried` > 0 на втором проходе — персистентность работает (бездолговые вычищаются после опроса)
- `reserve_blocked` — кандидаты, пропущенные из-за inactive/paused/grace-резервов (ликвидация по ним ревертит); `emode_unreadable` — eMode-категории не прочитаны (бонус fallback на резервный, оптимистичен для eMode-позиций)
- `cover_limit` в JSONL (collateral|dust) — покрытие урезано достаточностью коллатерала или анти-пылевым правилом $1000; прибыль считается с урезанного debt_to_cover_usd
- `logs/candidates_*.jsonl` — строки с `net_profit_usd` числом (не None)
- `data_unavailable < 5%` от `candidates_checked`
- `gas_assumed` в pass JSON: доля fallback-газа; если >50% — не нашлось ликвидатора с балансом+approve debtAsset (или estimateGas ревертит), оценка газа не реалистична (этап C пересмотрит)
- Нет дубликатов по user в JSONL (дедуп в logger, свежий HF в файле)
- Нет роста nonce — транзакции не шлются, ключей нет

## 4. Безопасность

- RPC ключ только в `/opt/aave-scanner/.env` (chmod 600), не в git
- Приватных ключей на сервере НЕТ. Stage B только чтение.
- SSH только по ключу, PasswordAuthentication no — настройка админа, вне setup.sh
- ufw: setup добавляет `allow 22/tcp`, но НЕ включает firewall автоматически:
  на shared-сервере default deny incoming оборвёт чужие inbound-сервисы.
  `sudo ufw enable` — осознанное решение админа.
- logrotate ставится setup.sh: daily, rotate 14, compress (окно гейта B->C);
  cursor.txt не ротацияется

## 5. Гейт B -> C

Вести 14 дней. Переход только если:
всего >=5 кандидатов с net_profit_usd > $3 И >=1 с > $10.
Иначе протокол не подходит — идти на Venus/Moonwell тем же кодом.
