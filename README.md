# TradingView → Binance USDⓈ-M Futures bridge

A small FastAPI execution bridge for TradingView webhooks and Binance USDⓈ-M Futures in **One-way Mode** (`positionSide=BOTH`). Signals use Binance-style `side: buy|sell` and `reduceOnly: true|false`. Orders are submitted as `LIMIT GTC` using the webhook's `price`; for opening orders with `investmentType: notional_value`, `amount` is the quote notional value and base quantity is calculated as `amount / price`.

> Start with `DRY_RUN=true`. This software can place real limit and market orders when dry-run is disabled. Review it, use Binance's testnet first, restrict the API key to futures trading (never withdrawals), and add an IP restriction where possible.

## Safety model

- SQLite event IDs prevent duplicate execution. Successful duplicates return 200; processing duplicates return 409; failed events require `"retry": true`.
- A per-symbol lock prevents concurrent signals in one Uvicorn worker from racing on the same position. The supplied service deliberately uses one worker; scaling to multiple processes requires a cross-process lock or queue.
- Reversals cancel open orders, close the full opposite position with `reduceOnly=true`, poll until flat, and abort on timeout rather than opening anyway.
- Limit price and calculated base quantity are rounded down to Binance `PRICE_FILTER.tickSize` and `LOT_SIZE.stepSize`, then checked against applicable filters.
- Opening orders require absolute `stopLossPrice` and `takeProfitPrice`. After the first fill, the worker cancels any unfilled entry remainder, reads the live position, and installs exchange-side `STOP_MARKET` and `TAKE_PROFIT_MARKET` Algo orders with `closePosition=true`.
- An opening LIMIT order that remains completely unfilled for `ENTRY_ORDER_TIMEOUT_SECONDS` (1800 seconds / 30 minutes by default) is canceled. If it fills while cancellation is in flight, the resulting position is detected and protected instead of being abandoned.
- A reduce-only SELL cancels pending non-reduce-only BUY long-opening orders before reducing a long; a reduce-only BUY cancels pending non-reduce-only SELL short-opening orders before reducing a short. Manual reduce and replacement-entry signals remove older Algo protection first.
- Bracket state is persisted in SQLite and reconciled after process restarts. Once the position is flat, the worker cancels the remaining sibling Algo order so it cannot affect a future position.
- If a non-reduce-only signal arrives while Binance still holds the opposite position, the bridge cancels open orders, closes that old position with a reduce-only MARKET order, confirms flat, then submits the new LIMIT order.
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
    "side":"buy",
    "positionSide":"BOTH",
    "investmentType":"notional_value",
    "price":40000,
    "amount":"80",
    "reduceOnly":false,
    "stopLossPrice":"39000",
    "takeProfitPrice":"41000",
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
3. Test open, partial fill, stop-loss, take-profit, sibling cancellation, close, reversal, duplicate, failed retry, and restart recovery on testnet.
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

// side: "buy" / "sell"
// reduceOnly: true = reduce/close, false = open/add
f_msg(side, reduceOnly, open_price, stop_loss_price, take_profit_price) =>
    reduce_str = reduceOnly ? "true" : "false"
    msg = '{"token":"' + webhook_token + '"'
    msg := msg + ',"event_id":"' + syminfo.ticker + '_' + side + '_' + str.tostring(time) + '_' + str.tostring(bar_index) + '"'
    msg := msg + ',"symbol":"' + syminfo.ticker + '"'
    msg := msg + ',"side":"' + side + '"'
    msg := msg + ',"positionSide":"BOTH"'
    msg := msg + ',"investmentType":"notional_value"'
    msg := msg + ',"amount":"' + binance_amount + '"'
    msg := msg + ',"price":"' + str.tostring(open_price) + '"'
    msg := msg + ',"reduceOnly":' + reduce_str
    if not reduceOnly
        msg := msg + ',"stopLossPrice":"' + str.tostring(stop_loss_price) + '"'
        msg := msg + ',"takeProfitPrice":"' + str.tostring(take_profit_price) + '"'
    msg := msg + ',"source":"tradingview"'
    msg := msg + ',"strategy":"insititue_price_action"'
    msg := msg + '}'
    msg
```

Use it in the strategy like this:

```pine
strategy.entry("初始空单", strategy.short, stop=short_open_price,
     alert_message=f_msg("sell", false, short_open_price,
                         short_stop_price, short_profit_price))

strategy.entry("初始多单", strategy.long, stop=long_open_price,
     alert_message=f_msg("buy", false, long_open_price,
                         long_stop_price, long_profit_price))

strategy.exit("多单平仓", "初始多单", stop=long_stop_price,
     limit=long_profit_price, alert_message=f_msg("sell", true, close, 0.0, 0.0))

strategy.exit("空单平仓", "初始空单", stop=short_stop_price,
     limit=short_profit_price, alert_message=f_msg("buy", true, close, 0.0, 0.0))
```

The webhook `price` now comes from the explicit `open_price` argument, not `close`. For a long entry, the bridge requires `stopLossPrice < open_price < takeProfitPrice`. For a short entry, it requires `takeProfitPrice < open_price < stopLossPrice`. Reduce-only alerts still need an execution price, so the fallback exit examples pass `close`; they do not need protection fields, and the `0.0` protection arguments are not serialized.

Signal mapping:

- `buy + reduceOnly=false`: open/add long
- `sell + reduceOnly=false`: open/add short
- `sell + reduceOnly=true`: reduce long
- `buy + reduceOnly=true`: reduce short

All webhook orders are LIMIT GTC. A reduce-only order may remain pending until its price is reached. For `reduceOnly=true`, webhook `amount` and legacy `notional` are ignored for execution quantity: the bridge reads Binance's live `positionAmt` and submits the entire matching position quantity, so TradingView sizing drift cannot leave a partial position.

The entry LIMIT order is monitored by the server; TradingView does not need to send a later exit signal for protection. On first partial fill, the worker cancels the unfilled remainder and protects the resulting fixed position. If no fill occurs within `ENTRY_ORDER_TIMEOUT_SECONDS=1800` (30 minutes), the worker cancels the stale entry. Protection triggers use `MARK_PRICE` by default. `ALGO_PRICE_PROTECT=false` avoids delaying an emergency trigger because mark and contract prices temporarily diverge.

Only the required execution fields are modeled. Extra TradingView or legacy fields such as `positionMode`, `action`, `notional`, and any unknown metadata are accepted and ignored; they never override `side`, `amount`, `price`, or `reduceOnly`.

In the TradingView alert dialog:

- Condition: your strategy
- Trigger: **Order fills only**
- Webhook URL: `https://YOUR_DOMAIN/webhook/tradingview`
- Message: `{{strategy.order.alert_message}}`

Pass the generated message as your Pine strategy order's `alert_message` value.

## Event inspection and recovery

```bash
sqlite3 bridge.db 'select event_id,received_at,symbol,side,reduce_only,price,amount,status,error from events order by id desc limit 20;'
sqlite3 bridge.db 'select event_id,symbol,entry_order_id,status,stop_algo_id,take_profit_algo_id,error from brackets order by id desc limit 20;'
```

If an event failed, first inspect Binance's actual position and the service logs. Resend the identical payload with `"retry": true` only when it is safe. Never change the meaning of an existing `event_id`.

The primary endpoints used are `POST /fapi/v1/order`, `GET /fapi/v1/order`, `POST /fapi/v1/algoOrder`, `DELETE /fapi/v1/algoOrder`, `DELETE /fapi/v1/algoOpenOrders`, `GET /fapi/v3/positionRisk`, `GET /fapi/v1/positionSide/dual`, `GET /fapi/v1/openOrders`, `DELETE /fapi/v1/order`, `DELETE /fapi/v1/allOpenOrders`, and `GET /fapi/v1/exchangeInfo`.
