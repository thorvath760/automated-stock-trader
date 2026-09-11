# Liquidity Sweep Paper Trader

A Python/Streamlit stock-trading dashboard and continuous scanner for **Alpaca paper trading**. It detects liquidity sweeps that reclaim a prior rolling high/low, confirms above-average volume, optionally filters by EMA trend, sizes each position by defined account risk, and submits bracket orders.

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

## Before considering live trading

Run paper trading for an extended period, compare fills to backtests, add alerts and process monitoring, test disconnect/restart behavior, confirm day-trading and margin restrictions for your account, and review the strategy with a qualified financial professional. Live mode is intentionally not included in this build.

