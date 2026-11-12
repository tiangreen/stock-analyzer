# Stock Analyzer

A local dashboard that tracks a watchlist of stocks, computes technical indicators from free
market data, and once a day asks an Anthropic or GPT model to analyze each stock and forecast
its short-term trend. Forecasts are graded against what actually happened, so you can see how
reliable each model has been.

The models are reached through your **subscriptions**, not API keys:

| Provider | CLI used | Sign in |
|----------|----------|---------|
| Claude (Anthropic) | `claude` (Claude Code) | run `claude`, then `/login` |
| GPT (OpenAI) | `codex` (Codex CLI) | `codex login` with your ChatGPT account |

API-key environment variables are removed from the CLI calls so usage always goes to the
subscription login.

**Sign in from the dashboard**: the header shows whether the selected provider is signed in.
**Sign in** starts the CLI's own login in the background, which opens the provider's authorize
page in your browser; the dashboard also shows the link in case the page did not open. If the
page gives you a one-time code instead of finishing by itself, paste it into the code field.
The badge turns to "Signed in" once the CLI has the login. Credentials are entered only on the
provider's page and never pass through this app.

## Start

Windows (PowerShell):

```powershell
.\run.ps1
```

Linux or WSL2:

```bash
./run.sh
```

Then open http://127.0.0.1:8765. The first start creates the virtual environment and installs
dependencies.

### WSL2

The app runs in WSL2 from the same checkout as on Windows (for example
`/mnt/g/Projects/stock-analyzer`), and the dashboard opens in the Windows browser at the same
address, because WSL2 forwards `localhost`. Tested on Ubuntu 26.04 with Python 3.14.

One-time setup inside WSL:

1. Python 3.11 or newer with the venv module: `sudo apt install python3-venv`.
2. The model CLIs, installed **inside WSL**. WSL puts the Windows `PATH` after its own, so
   `claude` and `codex` may already resolve to the Windows installs, but those cannot be used
   from Linux and the app ignores them:

   ```bash
   npm install -g @anthropic-ai/claude-code @openai/codex
   ```

   If npm then asks for root, point it at your home directory first
   (`npm config set prefix ~/.npm-global`); `run.sh` adds `~/.npm-global/bin` and
   `~/.local/bin` to `PATH` by itself.
3. Sign in once per CLI (`claude auth login`, `codex login`) or with the dashboard's
   **Sign in** button. WSL keeps its own logins, separate from the Windows ones. If no browser
   opens from WSL, use the link the dashboard shows.

Good to know:

- **Where the environment lives**: when the project is on a Windows drive, `run.sh` keeps the
  Linux environment in `~/.venvs/stock-analyzer`, because `.venv` in the project belongs to
  Windows and Windows drives are slow from WSL. On a Linux filesystem it uses `.venv`. Set
  `STOCK_ANALYZER_VENV` to choose another place.
- **One server at a time**: Windows and WSL share `config.yaml` and `data/analyzer.db` when
  they use the same checkout. Stop the Windows server before starting the WSL one (and the
  other way round). Two servers would both fire the daily run, and a server marks an
  unfinished run as interrupted when it starts. Also, if the Windows server holds port 8765,
  `localhost:8765` in the Windows browser shows that one, not the WSL server.
- **Schedule**: the daily run only fires while the WSL distribution is running. WSL shuts an
  idle distribution down when its last terminal closes, so keep the terminal with `run.sh`
  open. The time is local time, which WSL takes from Windows.

- **Watchlist**: type a company name or code in the add box and pick from the suggestions, or
  enter a Yahoo Finance symbol directly. US tickers as-is (`AAPL`); other markets take a suffix:

  | Market | Suffix | Example |
  |--------|--------|---------|
  | Korea KOSPI / KOSDAQ | `.KS` / `.KQ` | `005930.KS` Samsung Electronics |
  | Japan (Tokyo) | `.T` | `7203.T` Toyota |
  | Germany, Paris, Amsterdam | `.DE`, `.PA`, `.AS` | `SAP.DE`, `MC.PA`, `ASML.AS` |
  | London, Milan, Zurich | `.L`, `.MI`, `.SW` | `SHEL.L`, `NESN.SW` |

- **Open in TradingView** opens the selected stock's chart on tradingview.com in your own
  logged-in session (see "TradingView" below).
- **Currency**: prices are shown in EUR by default, switchable to USD in the header. Every
  price (watchlist, charts, indicators, forecast targets) is converted from the stock's trading
  currency at the latest exchange rate; the stock header also shows the original price.
  Percentage changes stay as they are in the trading currency, so they exclude currency moves.
  The model analyzes in the trading currency, so figures quoted inside its text are unconverted.
- **Model**: pick Claude or GPT in the header. Model names per provider are set in `config.yaml`.
- **Daily at**: the time (local) of the automatic weekday run and a checkbox to pause it.
  The schedule only fires while the server is running; if the server starts after that time
  and no run has happened yet, it catches up immediately.
- **Run now** analyzes the whole watchlist; **Analyze now** redoes the selected stock.
- **Signals** lists the latest trade signal (buy, sell or hold) per stock and how past signals
  turned out (see "Trade signals" below).
- **Accuracy** shows hit rates per model, horizon and stock. **Runs** shows the run log.

Run from the command line instead:

```powershell
.\.venv\Scripts\python.exe -m stock_analyzer.runner                      # whole watchlist
.\.venv\Scripts\python.exe -m stock_analyzer.runner --ticker NVDA --provider codex --force
```

On Linux or WSL2 the interpreter is `.venv/bin/python`, or `~/.venvs/stock-analyzer/bin/python`
when the project is on a Windows drive:

```bash
~/.venvs/stock-analyzer/bin/python -m stock_analyzer.runner --ticker NVDA --provider codex --force
```

## How it works

1. **Data** (`data.py`): daily bars, company fundamentals and headlines from Yahoo Finance via
   `yfinance`, cached in SQLite (`data/analyzer.db`). Prices are adjusted for splits and dividends.
2. **Indicators** (`indicators.py`): SMA 20/50/200, EMA 12/26, RSI 14, MACD, Bollinger bands,
   ATR 14, Stochastic, OBV, volume ratio, 52-week range, plus derived signals (golden/death
   cross, MACD crossover, overbought/oversold, band breaks, volume spikes).
3. **Forecast** (`prompt.py`, `llm/`): the model gets the last 60 bars, the indicator snapshot,
   fundamentals, headlines and its own earlier forecasts with their outcomes. It returns a trend,
   summary, signals, risks, support/resistance and a direction, confidence and target range for
   each horizon (default 1, 5 and 20 trading days).
4. **Scoring** (`scoring.py`): once a horizon has passed (counted in real trading sessions), the
   forecast is graded: direction hit or miss, and whether the close landed in the target range.
   A move counts as "flat" inside ±0.5% for one day, widening with the square root of the horizon.

## Trade signals

Each analysis also asks the model for one trade signal, meant for placing orders **by hand** at
your broker (for example Trade Republic, which offers no trading API). This app never places an
order and has no connection to any broker.

- The model proposes `buy`, `sell` (exit or reduce a position you hold) or `hold`, with a
  confidence, a limit price and, for a buy, a stop-loss and take-profit.
- Fixed rules (`signals.py`) then check the proposal against the `signals` section of
  `config.yaml`: minimum confidence, limit close to the last close, agreement with the forecast
  for the same horizon and, for a buy, a maximum stop distance and a minimum reward/risk. A
  proposal that fails any check is shown as `hold`, with the reason.
- A buy gets a suggested position size: the share of the portfolio at which hitting the stop
  loses `risk_per_trade_pct`, capped at `max_position_pct`. Set `portfolio_value` to see it as
  an amount.
- The Signals tab replays past buy and sell signals on the daily bars that followed (filled or
  not, stopped, target reached, or expired at the end of the horizon). Fees, taxes and price
  gaps are ignored, so treat the record as a rough guide.

Prices are the previous close from Yahoo Finance; check the live price before ordering.

## Notes

- Markets close at different times (Seoul and Tokyo in the European morning, Europe in the
  late afternoon, the US at night). A bar from a session that is still open is left out, so
  every stock is analyzed and graded on its last finished day whenever the run happens. The
  default 22:30 suits Central European time: all of these markets are closed by then.

## TradingView

A TradingView subscription cannot serve as this app's data source: TradingView offers no data
API to subscribers and its terms forbid automated data collection (scripts, scraping,
unofficial APIs), with account bans as the penalty. Its REST API is for brokers only. So the
analysis keeps using Yahoo Finance, and the integration is the **Open in TradingView** link,
which is where the subscription pays off: your saved layouts and indicators, alerts, more
history, and real-time quotes for the exchanges whose data packages you have (Yahoo's quotes
are delayed for most non-US markets).
- Yahoo Finance data is free and unofficial: quotes can be delayed by 15 minutes or more and
  the service occasionally rate-limits.
- Each analysis is one model call per stock per day, counted against your subscription limits.
- Output is informational only and is not investment advice. Language models cannot reliably
  predict prices; use the Accuracy tab to judge how much weight the forecasts deserve.

## Troubleshooting

- **`ImportError: DLL load failed ... Anwendungssteuerungsrichtlinie hat diese Datei blockiert`**
  (an application control policy blocked this file): Windows Smart App Control refused one of
  pandas' unsigned DLLs. This happened once with pandas 3.1.1 and went away after a fresh
  download:

  ```powershell
  .\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps --no-cache-dir pandas
  ```

  If the block stays, install an older release instead (3.0.6 worked):
  `.\.venv\Scripts\python.exe -m pip install "pandas==3.0.6"`.
- **Port 8765 already in use**: another copy of the server is running. Stop it, or change
  `server.port` in `config.yaml`.
- **WSL: "CLI not installed" although `claude` or `codex` works in the WSL terminal**: the
  command found is the Windows install (`which claude` shows a path under `/mnt/c/`). Install
  the CLI inside WSL, see "WSL2" above.
- **WSL: `No module named 'ensurepip'` or "Python's venv module is missing"**:
  `sudo apt install python3-venv`.
- **WSL: `./run.sh` fails with `bad interpreter` or `$'\r': command not found`**: the script
  was checked out with Windows line endings. `.gitattributes` prevents that for new checkouts;
  for an existing one run `git add --renormalize . && git checkout -- run.sh`.
- **No signal on a stock**: analyses made before trade signals existed carry none. Use
  **Analyze now** or **Run now**.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

On Linux or WSL2 (with the interpreter path from above):

```bash
~/.venvs/stock-analyzer/bin/python -m pip install -e ".[dev]"
~/.venvs/stock-analyzer/bin/python -m pytest
```

## License

The code is released under the [MIT License](LICENSE). Dependencies keep their own licenses,
and market data and model output are not covered by it: see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
