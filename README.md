# Stock Analyzer

A local dashboard that tracks a watchlist of stocks, computes technical indicators from free
market data, and once a day asks an Anthropic or GPT model to analyze each stock and forecast
its short-term trend. Forecasts are graded against what actually happened, so you can see how
reliable each model has been.

The models are reached through your **subscriptions**, or optionally through an API with a
key or token (see "API providers" below). The subscriptions:

| Provider | CLI used | Sign in |
|----------|----------|---------|
| Claude (Anthropic) | `claude` (Claude Code) | run `claude`, then `/login` |
| GPT (OpenAI) | `codex` (Codex CLI) | `codex login` with your ChatGPT account |

API-key environment variables are removed from the CLI calls so these two always use the
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

To stop the app, run `.\run.ps1 stop` (Windows) or `./run.sh stop` (Linux, WSL2) from any
terminal, use **Stop app** in the dashboard's Settings tab (click twice to confirm), or press
Ctrl+C in the terminal it runs in. The stop command asks the running app to shut down cleanly
and ends its process if it does not react. The daily schedule only runs while the app does.

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
- **Language**: the dashboard is available in English, German and Chinese; switch it in the
  header (default: `language` in `config.yaml`). The model writes the text of new analyses in
  the selected language; stored analyses keep the language they were written in. Run logs
  and error messages from the server and the CLIs stay in English. Translations live in
  `src/stock_analyzer/static/i18n.js`.
- **Currency**: prices are shown in EUR by default, switchable to USD in the header. Every
  price (watchlist, charts, indicators, forecast targets) is converted from the stock's trading
  currency at the latest exchange rate; the stock header also shows the original price.
  Percentage changes stay as they are in the trading currency, so they exclude currency moves.
  The model analyzes in the trading currency, so figures quoted inside its text are unconverted.
- **Model**: pick the provider in the header: Claude or GPT through your subscription, or an
  API provider. Each provider's model and effort are chosen in the Settings tab.
- **Daily at**: the time (local) of the automatic weekday run and a checkbox to pause it.
  The schedule only fires while the server is running; if the server starts after that time
  and no run has happened yet, it catches up immediately.
- **Run now** analyzes the whole watchlist; **Analyze now** redoes the selected stock.
- **Refresh / 10s / 30s** (under the stock's title) fetch the live price of the session in
  progress, once or automatically every 10 or 30 seconds. Auto-refresh is on by default at
  30 seconds, with a bar counting down to the next refresh; click the active button again to
  stop, and the choice is remembered in the browser. The quote and today's forming candle are display only: nothing is stored and no model
  runs. Auto-refresh waits while the page is hidden and slows down while the exchange is closed.
- **Realtime** (checkbox beside Analyze now) analyzes the selected stock during its trading
  session: the model also gets the live price, today's bar so far and its 15-minute bars, and
  forecasts from the live price with today's close as the first horizon. It works only while
  the stock's exchange is open, and Yahoo quotes can be delayed (often 15 minutes). A realtime
  analysis is stored beside the after-close one and marked as such; its forecasts and signals
  are not graded, so Accuracy and the signal record stay close-to-close. The daily run and
  Run now always analyze the last finished session.
- **Signals** lists the latest trade signal (buy, sell or hold) per stock and how past signals
  turned out (see "Trade signals" below).
- **Discover** recommends stocks you do not track yet, short-term and long-term (see
  "Discover" below).
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

## API providers

Besides the subscriptions, the app can call models over HTTPS with an API key or a token.
These are separate providers with their own entry in the header's model list, their own card
in Settings and their own forecast track record. Usage is billed per token by the service.

| Provider | Type | Credentials |
|----------|------|-------------|
| `anthropic-api` (built in) | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `openai-api` (built in) | OpenAI chat completions | `OPENAI_API_KEY` |
| any name you add | any OpenAI-compatible service or gateway | an API key variable you name, or OAuth2 client credentials |

**Secrets come only from environment variables.** `config.yaml` holds the endpoint and the
*names* of the variables, never a key. Set the variables in the terminal that starts the app
(or permanently for your user), then start the app:

```powershell
$env:ANTHROPIC_API_KEY = "..."      # this terminal only
.\run.ps1
```

```bash
export ANTHROPIC_API_KEY=...
./run.sh
```

The Settings card shows whether the variable was found (never its value). Pick the model
there; `anthropic-api` defaults to `claude-opus-5-5`, an OpenAI-type provider needs a model
chosen in Settings or set as `model` in its entry.

**Other services and M2M gateways** are added under `api_providers` in `config.yaml`; the
commented example there shows every field. An entry with an `oauth` section uses the OAuth2
client-credentials grant: the app posts the client ID and secret (from the two variables the
entry names) to `token_url`, keeps the access token in memory until shortly before it
expires, sends it as `Authorization: Bearer ...`, and fetches a new one if the API answers
401. `auth_style` chooses whether the client ID and secret travel in the Basic header or the
form body. Endpoints must be `https://` (`http://` only for localhost), and can be changed
only in `config.yaml`, not from the dashboard.

Good to know:

- **Answer format**: `response_format: json_schema` makes the service enforce the answer's
  shape. It is the default for `anthropic-api` and `openai-api`; custom endpoints default to
  `none` because many do not support it, which is fine: the prompt asks for JSON only.
- **Declined requests**: on `anthropic-api`, a request the chosen model declines for safety
  is answered by Anthropic's recommended fallback model in the same call (current Fable, Opus
  and Sonnet 5.5 models). The analysis is still stored under the model you chose.
- **Already installed?** The API providers need new Python packages. Run
  `.venv\Scripts\python.exe -m pip install -e .` once (Linux, WSL: the same with your
  environment's `python`), or delete `.venv` and start the app again.
- The subscription providers are unchanged: `claude` and `codex` still drop API-key variables
  from the CLI calls, so setting a key never moves subscription usage to API billing.

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

## Discover

The **Discover** tab finds stocks outside the watchlist and has the model recommend up to five
for the **short term** (1 to 4 weeks) and five for the **long term** (6 to 24 months).
**Find stocks** starts a search; it takes a few minutes and two model calls. Nothing runs by
schedule.

**Markets to search**: tick one market or any combination (US, Germany, France, Netherlands,
Switzerland, UK, Japan, Korea). The choice is remembered. Stocks are ranked inside their own
market first, so with one market ticked the shortlists hold only that market's stocks, and
with several the best of each compete. `discovery.regions` in `config.yaml` sets which
markets are on offer.

From the command line: `python -m stock_analyzer.discovery [--provider codex] [--region de
--region fr]`. Without `--region` it searches the markets ticked in the dashboard.

The model CLIs have no web access, so the model cannot research the market itself. Code
collects and scores the evidence, the model chooses, and fixed rules check its choice:

1. **Universe**: Yahoo's screener lists the largest stocks on the home exchanges of each
   selected market above a market cap and a daily traded value. Regional screens are full of
   foreign cross-listings (Apple on Xetra); those are dropped because they hardly trade
   compared with their market cap (`min_turnover`). Share classes and double listings collapse
   to the most traded line. About 800 stocks.
2. **Price factors** for all of them from one year of daily bars: returns over 1, 3 and 6
   months, 12-1 month momentum, strength against the home index, trend against the 50- and
   200-day averages, distance to the 52-week high, volatility, drawdown, RSI, unusual volume.
3. **Finalists**: a first ranking inside each region (a stock competes with its own market)
   selects 40 stocks per category. For the long term it leans on fundamentals: two more
   screens flag quality (return on equity, low debt, margin) and growth (earnings and revenue).
4. **Full data** for the finalists: margins, return on equity, free cash flow, debt, valuation
   ratios, analysts' EPS estimates and how they changed over 30 and 90 days, earnings
   surprises, rating changes, the next earnings date and headlines.
5. **Scores**: each pillar is a percentile from 0 to 100 among the finalists; the weights are
   in `config.yaml`.

   | Long-term | Weight | | Short-term | Weight |
   |-----------|--------|-|------------|--------|
   | Quality (ROE, margins, cash flow, leverage) | 30 | | Momentum (vs index, trend, 52-week high) | 30 |
   | Valuation (earnings and cash-flow yield, EV/EBITDA, PEG; against sector peers) | 25 | | Revisions (EPS estimates and ratings, 30 days) | 25 |
   | Growth (revenue, earnings, forward estimates) | 20 | | Earnings drift (last surprise, fading with time) | 15 |
   | Estimates (90-day revisions, analyst target) | 10 | | Volume (unusual volume on rising prices) | 10 |
   | Trend (12-1 momentum, 200-day average) | 10 | | Catalyst (earnings date ahead, fresh news) | 10 |
   | Risk (volatility, beta, drawdown) | 5 | | Overextension (penalty for a stretched price) | 10 |

   Stocks with losses and no free cash flow, with debt above three times equity (financials
   excepted) or without fundamentals data are excluded. The best 12 per category, at most 4
   from one sector, form the shortlist.
6. **The model's choice**: per category the model gets the shortlist with all scores and
   figures, each company's description and headlines, and the market regime (each index
   against its 200-day average, VIX, US 10-year yield). It is asked to weigh what the scores
   cannot see, such as one-off earnings, value traps, event risk and how durable the business
   is, and may recommend fewer than five stocks or none. For each pick it gives a conviction,
   a thesis, catalysts, risks, what would prove it wrong and a note on the entry.
7. **Rules** (`discovery.review`): a pick must be on the shortlist, reach `min_conviction`,
   and there are at most two picks per sector and five per category. What the rules dropped
   is listed with the reason.

Each pick shows its score and pillar bars, and **Add to watchlist** puts it into the daily
analysis. The unfolded shortlist table shows every candidate with its scores and why the model
passed it over.

**Track record**: a pick stores its price on the day it was made and is graded against its
home index (S&P 500, DAX, CAC 40, AEX, SMI, FTSE 100, Nikkei 225, KOSPI) after 20 trading
days (short-term) or 120 (long-term); until then the tab shows the return so far. Returns are
price changes in the stock's own currency; fees, taxes and currency moves are ignored.

Limits: scores are ranks among the candidates, not valuations. Yahoo's data is unofficial,
sometimes wrong and thinner outside the US (the Data column shows how much was available).
The model sees only what the prompt contains, and its own knowledge of a company can be out
of date. Treat a pick as a starting point for your own research.

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
- Each analysis is one model call per stock per day, counted against your subscription limits
  (or billed per token with an API provider). A Discover search is two more.
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
