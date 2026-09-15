# Liquidity Sweep Paper Trader

A Python/Streamlit stock-trading dashboard and continuous scanner for **Alpaca paper trading**. It detects liquidity sweeps that reclaim a prior rolling high/low, confirms above-average volume, optionally filters by EMA trend, sizes each position by defined account risk, and submits bracket orders.

The dashboard includes an educational risk assessment. In optional client-portal mode, users sign in with Google, accept the current paper-trading disclosure, and store their own assessment and settings in Supabase. It is not a substitute for professional investment advice or a regulated suitability review.

## Client signup setup

1. Create a Supabase project, open **SQL Editor**, paste `supabase_schema.sql`, and click **Run**.
2. Create a Google OAuth web client. Add your Streamlit app URL as an authorized origin and `https://YOUR-APP.streamlit.app/oauth2callback` as an authorized redirect URI.
3. Copy `.streamlit/secrets.example.toml` into Streamlit Cloud under **Manage app > Settings > Secrets**, replace every placeholder, and save. Do not upload a real secrets file to GitHub.
4. In `admin_emails`, enter only the email addresses allowed to see the existing owner Alpaca account and trading controls.
5. Reboot the Streamlit app. New users must sign in and accept the disclosure before entering.

The Supabase service-role key is server-only and bypasses Row Level Security. This build always scopes client queries on the trusted Streamlit server, but a public commercial launch should place database and Alpaca OAuth operations behind a dedicated backend API. Individual Alpaca OAuth is deliberately shown as unavailable until you register the app with Alpaca; never collect client passwords or brokerage API secrets in a form.

## Safety defaults

- Paper trading is hard-locked in code (`paper=True` and `ALPACA_PAPER=true`).
- Long-only by default; short selling is disabled.
- Risk per trade, maximum allocation, maximum daily loss, and maximum daily trade count are capped.
- Existing positions/open orders prevent another order in the same symbol.
- Every entry uses a linked stop-loss and take-profit bracket.
- API credentials stay in `.env`; never commit or share that file.

This is educational software, not financial advice. Backtests omit slippage, fees, spread, rejected orders, halts, and many real-market effects. Paper performance does not predict live results. Supervise automated systems because connectivity or software failures can cause missing, duplicate, or erroneous orders.

## Windows setup

1. Install Python 3.11 or newer from <https://www.python.org/downloads/>. During installation, select **Add Python to PATH**.
2. Open this folder in File Explorer. Click the address bar, type `powershell`, and press Enter.
3. Create and activate an isolated environment:

   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

4. Install packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

5. Copy `.env.example` to `.env` and `config.example.json` to `config.json`:

   ```powershell
   Copy-Item .env.example .env
   Copy-Item config.example.json config.json
   ```

6. Create an Alpaca paper account/API key. Put the paper key and secret in `.env`.
7. Start the dashboard:

   ```powershell
   streamlit run app.py
   ```

8. After checking settings and backtests, run the continuous paper bot in a second PowerShell window:

   ```powershell
   .venv\Scripts\Activate.ps1
   python bot.py
   ```

Stop the bot with `Ctrl+C`. The computer and terminal must remain running.

## Signal definition

A bullish setup requires the current low to trade below the lowest low of the preceding N bars, then close back above that old low with a bullish candle and confirmed volume. The bearish rule mirrors it. If enabled, the EMA filter permits bullish signals above the EMA and bearish signals below it. Orders are entered only after the bar has closed; backtests enter at the next bar open.

## Extended-hours paper mode

Pre-market (4:00-9:30 a.m. ET) and after-hours (4:00-8:00 p.m. ET) are enabled by default for paper testing. Overnight trading remains disabled. Extended-hours entries are limit orders, expire after five minutes if unfilled, use half the normal risk, and do not short.

Alpaca does not provide the regular bracket behavior for this extended-hours path. The bot therefore monitors filled entries and submits a limit exit when the strategy stop or target is crossed. **Keep `bot.py`, the computer, and the internet connection running whenever an extended-hours position exists.** A stopped or sleeping computer means the software exit is not monitored. Review the Alpaca dashboard before stopping the bot.

## Recommended settings

The Backtest tab includes **Recommend settings**. It compares 108 pre-approved combinations over 60 training days and 20 separate validation days. A recommendation must meet the minimum validation trade count, total R, profit factor, and drawdown limits in the configuration file. The user must click **Apply recommended settings**; the program never loosens settings merely to create a signal and never changes risk per trade, position limits, daily loss limits, paper-only mode, or short-selling permissions.

## Before considering live trading

Run paper trading for an extended period, compare fills to backtests, add alerts and process monitoring, test disconnect/restart behavior, confirm day-trading and margin restrictions for your account, and review the strategy with a qualified financial professional. Live mode is intentionally not included in this build.
