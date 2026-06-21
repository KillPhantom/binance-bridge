# TradingView → Binance USDⓈ-M Futures bridge

A small FastAPI execution bridge for TradingView webhooks and Binance USDⓈ-M Futures in **One-way Mode** (`positionSide=BOTH`). Opening signals place `LIMIT GTC` orders using the webhook's `price` and `amount`. The bridge makes Binance's real position authoritative, closes an opposite position with a reduce-only market order, confirms the account is flat, and only then submits the requested opening order.

> Start with `DRY_RUN=true`. This software can place real limit and market orders when dry-run is disabled. Review it, use Binance's testnet first, restrict the API key to futures trading (never withdrawals), and add an IP restriction where possible.

## Safety model

- SQLite event IDs prevent duplicate execution. Successful duplicates return 200; processing duplicates return 409; failed events require `"retry": true`.
- A per-symbol lock prevents concurrent signals in one Uvicorn worker from racing on the same position. The supplied service deliberately uses one worker; scaling to multiple processes requires a cross-process lock or queue.
- Reversals cancel open orders, close the full opposite position with `reduceOnly=true`, poll until flat, and abort on timeout rather than opening anyway.
- Opening `price` and `amount` are rounded down to Binance `PRICE_FILTER.tickSize` and `LOT_SIZE.stepSize`, then checked against minimum quantity and notional filters.
- `close_long` cancels pending non-reduce-only BUY opening orders before closing; `close_short` does the same for SELL opening orders. Reduce-only and close-position protection orders are preserved.
- The webhook token is compared safely and is redacted before payload storage. API secrets are read only from the environment and are never logged.
- `DRY_RUN=true` makes no Binance HTTP calls and returns synthetic flat positions and symbol filters. Dry-run orders are illustrative only.

## Local setup

Python 3.11 or newer is required.

```bash
cd tv-binance-bridge
cp .env.example .env
chmod 600 .env
```

Change `WEBHOOK_SECRET` to a long random value. Leave the Binance credentials empty and `DRY_RUN=true` for the first local run.

Run the app (this creates `venv`, installs dependencies, and binds only to localhost):

```bash
chmod +x scripts/*.sh
./scripts/run_local.sh
```

Run tests in another terminal:

```bash
venv/bin/python -m pytest -q
```

Health and dry-run webhook checks:

```bash
curl http://127.0.0.1:8000/health

curl -i -X POST http://127.0.0.1:8000/webhook/tradingview \
  -H 'Content-Type: application/json' \
  -d '{
    "token":"YOUR_LOCAL_WEBHOOK_SECRET",
    "event_id":"manual_test_001",
    "symbol":"BTCUSDT",
    "action":"open_long",
    "price":40000,
    "amount":0.002,
    "source":"manual",
    "strategy":"smoke_test"
  }'
```

Use a new `event_id` for each intentional execution. Reusing a successful ID is a no-op.

## Binance configuration

Production is the default base URL:

```dotenv
BINANCE_BASE_URL=https://fapi.binance.com
```

For testnet, set the current Binance USDⓈ-M Futures testnet REST base URL in `BINANCE_BASE_URL`, add testnet credentials, and only then set `DRY_RUN=false`. Confirm the account is in One-way Mode before sending signals. The bridge rejects a returned position whose `positionSide` is not `BOTH`.

Before production:

1. Verify every symbol in `ALLOWED_SYMBOLS` exists on USDⓈ-M Futures.
2. Confirm leverage and margin type in Binance; this bridge does not change them.
3. Test open, close, reversal, duplicate, failed retry, and timeout behavior on testnet.
4. Keep Uvicorn at one worker unless execution is moved behind a durable, cross-process queue.
5. Set `DRY_RUN=false` only after those checks.

## Deploy to Vultr

Create the destination once:

```bash
ssh ubuntu@YOUR_SERVER_IP 'mkdir -p /home/ubuntu/tv-binance-bridge'
```

From the project directory, sync code without touching server secrets or the database:

```bash
SERVER_USER=ubuntu \
SERVER_HOST=YOUR_SERVER_IP \
SERVER_PATH=/home/ubuntu/tv-binance-bridge \
./scripts/deploy_rsync.sh
```

Install server packages and the virtual environment:

```bash
ssh ubuntu@YOUR_SERVER_IP
cd /home/ubuntu/tv-binance-bridge
chmod +x scripts/*.sh
./scripts/install_server.sh
```

The installer detects the project directory from its own location. If you pass a directory explicitly, use an absolute path such as `./scripts/install_server.sh /root/tv-binance-bridge/binance-bridge`; do not use `/~/...`.

Create `/home/ubuntu/tv-binance-bridge/.env` manually—deployment intentionally never copies or overwrites it:

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Install systemd:

```bash
sudo cp scripts/systemd_service.example /etc/systemd/system/tv-binance-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now tv-binance-bridge
sudo systemctl status tv-binance-bridge
journalctl -u tv-binance-bridge -f
```

Install Nginx, replacing `YOUR_DOMAIN` first:

```bash
sudo cp scripts/nginx.example /etc/nginx/sites-available/tv-binance-bridge
sudo ln -s /etc/nginx/sites-available/tv-binance-bridge /etc/nginx/sites-enabled/tv-binance-bridge
sudo nginx -t
sudo systemctl reload nginx
```

Configure DNS and HTTPS with your preferred certificate tooling before exposing the webhook. Then allow only SSH and Nginx through UFW; keep port 8000 private:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

After each deployment:

```bash
ssh ubuntu@YOUR_SERVER_IP 'sudo systemctl restart tv-binance-bridge'
curl https://YOUR_DOMAIN/health
```

## TradingView alert

Pine message builder:

```pine
webhook_token = "same_as_WEBHOOK_SECRET"

f_server_msg(action, order_price, order_amount) =>
    msg = '{"token":"' + webhook_token + '"'
    msg := msg + ',"event_id":"' + syminfo.ticker + '_' + action + '_' + str.tostring(time) + '_' + str.tostring(bar_index) + '"'
    msg := msg + ',"symbol":"' + syminfo.ticker + '"'
    msg := msg + ',"action":"' + action + '"'
    is_open = action == "open_long" or action == "open_short"
    if is_open
        msg := msg + ',"price":' + str.tostring(order_price)
        msg := msg + ',"amount":' + str.tostring(order_amount)
    msg := msg + ',"source":"tradingview"'
    msg := msg + ',"strategy":"my_strategy"'
    msg := msg + '}'
    msg
```

Example: `f_server_msg("open_long", close, 0.002)`. `price` and `amount` are required for `open_long` and `open_short`; close and flatten actions do not require them. Safety-driven closes remain reduce-only market orders so an unfilled limit close cannot leave an opposite position behind.

Use one of: `open_long`, `open_short`, `close_long`, `close_short`, or `flatten`.

In the TradingView alert dialog:

- Condition: your strategy
- Trigger: **Order fills only**
- Webhook URL: `https://YOUR_DOMAIN/webhook/tradingview`
- Message: `{{strategy.order.alert_message}}`

Pass the generated message as your Pine strategy order's `alert_message` value.

## Event inspection and recovery

```bash
sqlite3 bridge.db 'select event_id,received_at,symbol,action,status,error from events order by id desc limit 20;'
```

If an event failed, first inspect Binance's actual position and the service logs. Resend the identical payload with `"retry": true` only when it is safe. Never change the meaning of an existing `event_id`.

The primary endpoints used are `POST /fapi/v1/order`, `GET /fapi/v3/positionRisk`, `GET /fapi/v1/positionSide/dual`, `GET /fapi/v1/openOrders`, `DELETE /fapi/v1/order`, `DELETE /fapi/v1/allOpenOrders`, and `GET /fapi/v1/exchangeInfo`.
