"""Pull the quantitative anchor for a pitch from Yahoo Finance via yfinance.

The model does its own qualitative research with web search; this module supplies the
reported numbers it is not allowed to contradict.
"""

from __future__ import annotations

import json
from pathlib import Path
import logging
import subprocess
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import requests
import yfinance as yf

import config

# yfinance narrates failed symbol probes at error level; we handle those ourselves.
# That also hides a refused quote-summary call, so fetch() logs what came back itself.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
log = logging.getLogger("stockbot.market_data")

# Statement rows worth putting in front of the model, in the order they should read.
INCOME_ROWS = [
    "Total Revenue",
    "Cost Of Revenue",
    "Gross Profit",
    "Research And Development",
    "Selling General And Administration",
    "Operating Income",
    "EBITDA",
    "Interest Expense",
    "Net Income",
    "Diluted EPS",
]
BALANCE_ROWS = [
    "Cash Cash Equivalents And Short Term Investments",
    "Total Debt",
    "Net Debt",
    "Total Assets",
    "Goodwill And Other Intangible Assets",
    "Net PPE",
    "Stockholders Equity",
    "Minority Interest",
    "Working Capital",
]
CASHFLOW_ROWS = [
    "Operating Cash Flow",
    "Capital Expenditure",
    "Free Cash Flow",
    "Repurchase Of Capital Stock",
    "Depreciation",
    "Amortization Of Intangibles",
]

FINANCIAL_INCOME_ROWS = ["Net Interest Income", "Pretax Income", "Tax Provision",
                         "Preferred Stock Dividends", "Net Income Common Stockholders"]
FINANCIAL_BALANCE_ROWS = ["Net Loan", "Common Stock Equity", "Preferred Stock Equity",
                          "Tangible Book Value"]
FINANCIAL_CASHFLOW_ROWS = ["Cash Dividends Paid"]

INFO_FIELDS = [
    ("longName", "Company"),
    ("symbol", "Symbol"),
    ("exchange", "Exchange"),
    ("currency", "Currency"),
    ("sector", "Sector"),
    ("industry", "Industry"),
    ("country", "Country"),
    ("fullTimeEmployees", "Employees"),
    ("marketCap", "Market cap"),
    ("enterpriseValue", "Enterprise value"),
    ("sharesOutstanding", "Shares outstanding"),
    ("floatShares", "Float"),
    ("trailingPE", "Trailing P/E"),
    ("forwardPE", "Forward P/E"),
    ("priceToSalesTrailing12Months", "P/S (TTM)"),
    ("priceToBook", "P/B"),
    ("enterpriseToEbitda", "EV/EBITDA"),
    ("enterpriseToRevenue", "EV/Revenue"),
    ("profitMargins", "Net margin"),
    ("grossMargins", "Gross margin"),
    ("operatingMargins", "Operating margin"),
    ("ebitdaMargins", "EBITDA margin"),
    ("returnOnEquity", "ROE"),
    ("returnOnAssets", "ROA"),
    ("revenueGrowth", "Revenue growth (YoY)"),
    ("earningsGrowth", "Earnings growth (YoY)"),
    ("debtToEquity", "Debt/Equity"),
    ("currentRatio", "Current ratio"),
    ("beta", "Beta"),
    ("dividendYield", "Dividend yield"),
    ("payoutRatio", "Payout ratio"),
    ("targetMeanPrice", "Analyst target (mean)"),
    ("targetHighPrice", "Analyst target (high)"),
    ("targetLowPrice", "Analyst target (low)"),
    ("numberOfAnalystOpinions", "Analysts covering"),
    ("recommendationKey", "Street rating"),
    ("fiftyTwoWeekHigh", "52w high"),
    ("fiftyTwoWeekLow", "52w low"),
]
MONETARY_RATIOS = {"trailingPE", "forwardPE", "priceToSalesTrailing12Months", "priceToBook",
                   "enterpriseToEbitda", "enterpriseToRevenue"}

PCT_FIELDS = {
    "profitMargins", "grossMargins", "operatingMargins", "ebitdaMargins",
    "returnOnEquity", "returnOnAssets", "revenueGrowth", "earningsGrowth",
    "payoutRatio",
}

# Yahoo reports these already scaled to percent (DBS pays 4.05, meaning 4.05%), unlike
# every field above, which arrives as a fraction. Scaling them again put a 405% dividend
# yield in the prompt block the model is told it must not contradict.
ALREADY_PCT_FIELDS = {"dividendYield"}


class TickerNotFound(Exception):
    """Raised when a user's query cannot be resolved to a tradeable symbol."""


@dataclass
class MarketData:
    symbol: str
    name: str
    exchange: str
    currency: str
    price: float | None
    market_cap: float | None
    shares_outstanding: float | None
    prompt_block: str
    warnings: list[str] = field(default_factory=list)
    # The valuation bridge. Statements are in the reporting currency, which is not
    # always the trading one (SPOT reports in EUR and trades in USD; TSM in TWD).
    # Money amounts are in millions of the reporting currency, like the statements.
    reporting_currency: str = ""
    fx: float = 1.0                 # one unit of reporting currency in trading currency
    fx_date: str = ""               # the day `fx` was read
    cash: float = 0.0
    debt: float = 0.0
    cash_debt_known: bool = False
    revenue: float | None = None    # latest reported annual revenue
    ttm_revenue: float | None = None  # the last four reported quarters
    ttm_ebitda: float | None = None
    ttm_net_income: float | None = None
    # What lies between them, from the same four quarters: data_gates reads these when net
    # income comes out above EBITDA, to tell gains and tax credits from a misread.
    ttm_operating_income: float | None = None
    ttm_pretax_income: float | None = None
    ttm_tax: float | None = None
    analyst_target: float | None = None
    analyst_target_low: float | None = None
    analyst_target_high: float | None = None
    analyst_rating: str | None = None
    analyst_count: int | None = None
    analyst_rating_mean: float | None = None  # 1 strong buy .. 5 sell
    street_source: str = ""  # where the targets came from
    # Named brokers' latest targets (config.STREET_FIRMS). None means Yahoo refused the
    # lookup; an empty list means it answered and none of the firms cover the stock.
    firm_targets: "list[FirmTarget] | None" = None
    # Yahoo refused the quote summary: no targets, ratios or margins. Worth asking again.
    summary_refused: bool = False
    # Inputs to the house cost of capital (valuation.cost_of_capital).
    beta: float | None = None       # raw, before the Blume adjustment
    beta_source: str = ""
    risk_free: float | None = None  # the 10-year government yield, as a fraction
    risk_free_source: str = ""      # whose: the reporting currency's home market
    # Reported years, oldest first: (label, revenue, EBITDA), millions of the reporting
    # currency, None where the statement has no figure. For the PDF's history chart.
    history: list = field(default_factory=list)
    # Yahoo's own EV/EBITDA from the snapshot, on its basis rather than the code's.
    quoted_ev_ebitda: float | None = None
    # Outside owners' share of consolidated subsidiaries, at book value (millions). The
    # DCF counts all of those subsidiaries' cash flow, so the bridge takes their share off.
    minority_interest: float = 0.0
    # Long-term equity investments at book (reporting mn), added to equity by the bridge.
    equity_investments: float = 0.0
    # Operating leases Yahoo counted in Total Debt and `debt` leaves out (US GAAP filers).
    operating_leases: float = 0.0

    def __post_init__(self) -> None:
        if not self.reporting_currency:
            self.reporting_currency = self.currency

    @property
    def display(self) -> str:
        return f"{self.name} ({self.exchange}: {self.symbol})" if self.name else self.symbol


def _fmt_number(value) -> str:
    """Human-scale a raw number: 1.23bn, 456mn, 7,890."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n.a."
    if isinstance(value, str):
        return value
    v = float(value)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return f"{sign}{a / 1e12:,.2f}tn"
    if a >= 1e9:
        return f"{sign}{a / 1e9:,.2f}bn"
    if a >= 1e6:
        return f"{sign}{a / 1e6:,.1f}mn"
    if a >= 1000:
        return f"{sign}{a:,.0f}"
    if a >= 1:
        return f"{sign}{a:,.2f}"
    return f"{sign}{a:.4f}"


def _fmt_info_value(key: str, value) -> str:
    if value is None:
        return "n.a."
    if key in PCT_FIELDS and isinstance(value, (int, float)):
        return f"{value * 100:.1f}%"
    if key in ALREADY_PCT_FIELDS and isinstance(value, (int, float)):
        return f"{value:.1f}%"
    if isinstance(value, (int, float)):
        return _fmt_number(value)
    return str(value)


def _statement_table(df: pd.DataFrame | None, rows: list[str], title: str, periods: int = 4) -> str:
    """Render selected statement rows as a markdown table, values in millions."""
    if df is None or df.empty:
        return f"### {title}\n\nNot available from the data provider.\n"

    cols = list(df.columns)[:periods]
    headers = [str(c.date()) if hasattr(c, "date") else str(c)[:10] for c in cols]
    lines = [f"### {title} (values in millions of reporting currency)", ""]
    lines.append("| Line item | " + " | ".join(headers) + " |")
    lines.append("| --- " * (len(headers) + 1) + "|")

    present = 0
    for row in rows:
        if row not in df.index:
            continue
        present += 1
        cells = []
        for col in cols:
            raw = df.loc[row, col]
            if pd.isna(raw):
                cells.append("n.a.")
            elif row == "Diluted EPS":
                cells.append(f"{float(raw):,.2f}")
            else:
                cells.append(f"{float(raw) / 1e6:,.0f}")
        lines.append(f"| {row} | " + " | ".join(cells) + " |")

    if not present:
        return f"### {title}\n\nNot available from the data provider.\n"
    return "\n".join(lines) + "\n"


def _chart_snapshot(ticker) -> dict:
    """Price, size and name from the chart endpoint, keyed like ticker.info.

    Each field is read on its own: fast_info fetches lazily, and shares in particular
    comes from a separate request that can fail while the price succeeds.
    """
    fields = (("lastPrice", "price"), ("currency", "currency"), ("exchange", "exchange"),
              ("marketCap", "marketCap"), ("shares", "sharesOutstanding"))
    snapshot = {}
    for src, dest in fields:
        try:
            value = ticker.fast_info[src]
        except Exception:
            continue
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            snapshot[dest] = value

    # The chart response also names the company. Without it, a refused summary left
    # the model researching bare "FIX" — and it went looking for a "Fix Inc." rather
    # than Comfort Systems USA.
    try:
        meta = ticker.get_history_metadata() or {}
    except Exception:
        meta = {}
    for key in ("longName", "shortName", "fullExchangeName"):
        if meta.get(key):
            snapshot[key] = meta[key]
    return snapshot


# Yahoo's sharesOutstanding can count one share class of a dual-class company. GOOG's on
# 2026-09-29 was 5,527mn, the class C shares alone, against a $4.15tn market cap that
# covers all ~12,230mn; the pitch valued Alphabet at +11%, HOLD, where its own model gave
# -50%. The market cap covers every class, so a count this far from it is replaced.
SHARE_COUNT_TOLERANCE = 0.05
# A gap of 100x is a quote in minor units (pence against pounds), not a missing class.
SHARE_COUNT_MAX_FACTOR = 10.0


def share_count(shares: float | None, market_cap: float | None,
                price: float | None) -> tuple[float | None, str | None]:
    """The share count to value per share with, and a note when Yahoo's was replaced."""
    if not (market_cap and price and market_cap > 0 and price > 0):
        return shares, None
    implied = market_cap / price
    if not shares:
        return implied, None
    ratio = implied / shares
    if abs(ratio - 1) <= SHARE_COUNT_TOLERANCE or not (
            1 / SHARE_COUNT_MAX_FACTOR < ratio < SHARE_COUNT_MAX_FACTOR):
        return shares, None
    return implied, (f"the provider's share count of {shares / 1e6:,.1f}mn does not match its "
                     f"market cap at today's price ({implied / 1e6:,.1f}mn shares, "
                     f"{ratio:.2f}x), usually because it counts one share class; the "
                     "valuation uses market cap / price")


# Exchanges that quote in minor units: LSE prices in pence, Johannesburg in cents,
# Tel Aviv in agorot. The statements are in the major unit.
MINOR_UNITS = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}


def _fx_rate(reporting: str, trading: str) -> float | None:
    """One unit of the reporting currency, in the trading currency."""
    major = MINOR_UNITS.get(trading, trading)
    scale = 100.0 if trading in MINOR_UNITS else 1.0
    if reporting.upper() == major.upper():
        return scale
    try:
        rate = yf.Ticker(f"{reporting.upper()}{major.upper()}=X").fast_info["lastPrice"]
    except Exception as exc:
        log.warning("FX %s->%s unavailable: %s", reporting, major, exc)
        return None
    if not rate or (isinstance(rate, float) and math.isnan(rate)):
        return None
    return float(rate) * scale


def _latest(df: pd.DataFrame | None, row: str) -> float | None:
    """The most recent non-empty value of a statement row, in millions."""
    if df is None or df.empty or row not in df.index:
        return None
    for col in df.columns:
        raw = df.loc[row, col]
        if not pd.isna(raw):
            return float(raw) / 1e6
    return None


def operating_leases(symbol: str, df: pd.DataFrame | None) -> float:
    """The operating lease liabilities inside Yahoo's Total Debt, in millions, for a US GAAP
    filer; 0 otherwise. Their cost is already an operating expense, so in debt as well they
    are counted twice: ESLT's bridge of 2026-10-07 deducted USD 585mn of leases from an
    enterprise value whose cash flows had paid the rent. An IFRS filer's leases stay in
    debt, since IFRS 16 puts their cost below EBITDA."""
    leases = _latest(df, "Capital Lease Obligations")
    if not leases or leases <= 0:
        return 0.0
    import sec_xbrl
    share = sec_xbrl.operating_lease_share(symbol)
    return leases * share if share else 0.0


def _trailing_sum(df: pd.DataFrame | None, row: str, quarters: int = 4) -> float | None:
    """The sum of the latest `quarters` values of a quarterly row, in millions."""
    if df is None or df.empty or row not in df.index:
        return None
    values = [df.loc[row, col] for col in list(df.columns)[:quarters]]
    if len(values) < quarters or any(pd.isna(v) for v in values):
        return None
    return float(sum(values)) / 1e6


@dataclass
class FirmTarget:
    firm: str               # as Yahoo writes it: "JP Morgan", "Morgan Stanley"
    target: float | None    # latest price target, trading currency
    grade: str              # latest rating: "Overweight", "Buy", "Neutral"...
    date: str               # when that target was set, YYYY-MM-DD


def _firm_key(name: str) -> str:
    """'JP Morgan', 'JPMorgan' and 'J.P. Morgan' are one firm."""
    return re.sub(r"[^a-z]", "", name.lower())


def firm_targets(ticker, firms: list[str], months: int = 12) -> list[FirmTarget] | None:
    """The latest target and rating from each named firm, or None when Yahoo refuses.

    A firm that has not covered the stock in `months` is left out. A row with no target
    (a rating change without one) keeps the firm's previous target and the new rating.
    """
    try:
        history = ticker.upgrades_downgrades
    except Exception as exc:
        log.warning("analyst rating history unavailable: %s", exc)
        return None
    if history is None or history.empty or "Firm" not in history.columns:
        return None
    history = history.sort_index(ascending=False)
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    found: list[FirmTarget] = []
    for firm in firms:
        rows = history[history["Firm"].map(lambda f: _firm_key(str(f)) == _firm_key(firm))]
        rows = rows[rows.index >= cutoff]
        if rows.empty:
            continue
        latest = rows.iloc[0]
        with_target = rows[rows["currentPriceTarget"].fillna(0) > 0]
        target = float(with_target.iloc[0]["currentPriceTarget"]) if not with_target.empty else None
        found.append(FirmTarget(firm=str(latest["Firm"]), target=target,
                                grade=str(latest.get("ToGrade") or ""),
                                date=str(rows.index[0].date())))
    return found


def resolve_symbol(query: str) -> str:
    """Turn user input ('$aapl', 'apple', 'AAPL') into a Yahoo symbol."""
    cleaned = query.strip().lstrip("$").strip()
    if not cleaned:
        raise TickerNotFound("Empty query.")

    # Try a direct hit only when the query actually looks like a ticker — a mixed-case
    # word like "Palantir" is a company name, and probing it just logs a 404.
    looks_like_symbol = (
        re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", cleaned)
        and (len(cleaned) <= 5 or cleaned.isupper() or "." in cleaned)
    )
    if looks_like_symbol:
        candidate = cleaned.upper()
        ticker = yf.Ticker(candidate)
        try:
            info = ticker.info
            if info.get("regularMarketPrice") or info.get("currentPrice"):
                return candidate
        except Exception:
            pass
        # From a datacenter IP Yahoo can refuse the quote summary and the search
        # endpoint together: FIX came back "no listed equity" from Render while
        # resolving normally from home. The chart endpoint still answers, and has no
        # price for a symbol that does not exist.
        if _chart_snapshot(ticker).get("price"):
            return candidate

    try:
        quotes = yf.Search(cleaned, max_results=8).quotes or []
    except Exception as exc:  # network or provider failure
        raise TickerNotFound(f"Could not look up '{query}': {exc}") from exc

    for quote in quotes:
        if quote.get("quoteType") == "EQUITY" and quote.get("symbol"):
            return str(quote["symbol"])

    log.warning("Yahoo search found no equity for %r (%d results)", query, len(quotes))
    raise TickerNotFound(f"No listed equity matches '{query}'.")


BETA_INDEX = "^GSPC"
BETA_MIN_MONTHS = 24
TREASURY_10Y = "^TNX"          # quoted in percent: 4.3 means 4.3%
RISK_FREE_RANGE = (0.005, 0.10)


def _monthly_closes(symbol: str) -> pd.Series:
    closes = yf.Ticker(symbol).history(period="5y", interval="1mo", auto_adjust=True)["Close"]
    closes = closes.dropna()
    index = closes.index.tz_localize(None) if closes.index.tz is not None else closes.index
    closes.index = index.to_period("M")
    return closes[~closes.index.duplicated(keep="last")]


def chart_beta(symbol: str) -> float | None:
    """Five-year monthly beta against the S&P 500, from price history alone.

    The chart endpoint answers Render when the quote summary (and Yahoo's own beta) does
    not, and one method every run keeps the discount rate from moving with the source.
    """
    try:
        joined = pd.concat([_monthly_closes(symbol), _monthly_closes(BETA_INDEX)],
                           axis=1, join="inner").pct_change().dropna()
    except Exception as exc:
        log.warning("beta from price history failed for %s: %s", symbol, exc)
        return None
    if len(joined) < BETA_MIN_MONTHS:
        return None
    variance = joined.iloc[:, 1].var()
    if not variance:
        return None
    return float(joined.cov().iloc[0, 1] / variance)


# Long-term government yields for currencies other than the dollar: OECD monthly 10-year
# yields on FRED (no key; plain CSV). A company reporting in euros is forecast in euros,
# so its cash flows are discounted at a euro rate: SPOT on 2026-10-11 discounted EUR flows
# at the 5.2% Treasury, 1.8pp above the Bund, and came out SELL at -48%.
HOME_YIELD_SERIES = {
    "EUR": ("IRLTLT01DEM156N", "10-year German Bund"),
    "GBP": ("IRLTLT01GBM156N", "10-year UK gilt"),
    "JPY": ("IRLTLT01JPM156N", "10-year JGB"),
    "CAD": ("IRLTLT01CAM156N", "10-year Canada"),
    "CHF": ("IRLTLT01CHM156N", "10-year Swiss Confederation"),
    "AUD": ("IRLTLT01AUM156N", "10-year Australia"),
    "SEK": ("IRLTLT01SEM156N", "10-year Sweden"),
    "DKK": ("IRLTLT01DKM156N", "10-year Denmark"),
    "NOK": ("IRLTLT01NOM156N", "10-year Norway"),
    "KRW": ("IRLTLT01KRM156N", "10-year Korea"),
    "ILS": ("IRLTLT01ILM156N", "10-year Israel"),
    "MXN": ("IRLTLT01MXM156N", "10-year Mexico"),
}


def home_yield(currency: str) -> tuple[float, str] | None:
    """The reporting currency's 10-year government yield and its label, or None."""
    series = HOME_YIELD_SERIES.get((currency or "").upper())
    if not series:
        return None
    code, label = series
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": code}, timeout=20)
        r.raise_for_status()
        rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:]]
        month, raw = next((d, v) for d, v in reversed(rows) if v.strip() not in ("", "."))
        value = float(raw) / 100
    except Exception as exc:
        log.warning("%s yield unavailable: %s", label, exc)
        return None
    if not RISK_FREE_RANGE[0] <= value <= RISK_FREE_RANGE[1]:
        return None
    return value, f"{label}, {month[:7]}"


def treasury_yield() -> float | None:
    """The 10-year US Treasury yield as a fraction, or None if the quote looks wrong."""
    try:
        value = float(yf.Ticker(TREASURY_10Y).fast_info["lastPrice"]) / 100
    except Exception as exc:
        log.warning("10-year Treasury yield unavailable: %s", exc)
        return None
    return value if RISK_FREE_RANGE[0] <= value <= RISK_FREE_RANGE[1] else None


STREET_KEYS = ("targetLowPrice", "targetMeanPrice", "targetHighPrice",
               "numberOfAnalystOpinions", "recommendationMean", "recommendationKey")


def _snapshot() -> dict:
    """snapshot.json from the Stock Finder repo's `snapshot` branch: the local checkout
    (fetched first), else the GitHub API. Empty when neither is reachable."""
    repo = Path(config.STOCK_FINDER_DIR)
    if (repo / ".git").exists():
        try:
            subprocess.run(["git", "fetch", "-q", "origin", "snapshot"], cwd=repo,
                           timeout=30, capture_output=True)
            out = subprocess.run(["git", "show", "origin/snapshot:snapshot.json"], cwd=repo,
                                 timeout=30, capture_output=True, check=True)
            return json.loads(out.stdout)
        except Exception as exc:
            log.warning("snapshot.json from %s failed: %s", repo, exc)
    if config.GITHUB_TOKEN and config.SNAPSHOT_REPO:
        try:
            r = requests.get(f"https://api.github.com/repos/{config.SNAPSHOT_REPO}/contents/"
                             "snapshot.json?ref=snapshot", timeout=30,
                             headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                                      "Accept": "application/vnd.github.raw"})
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("snapshot.json from GitHub failed: %s", exc)
    return {}


def street_targets(symbol: str) -> tuple[dict, str]:
    """The Street's targets keyed like ticker.info, and where they came from: Yahoo's
    quote summary, else the Stock Finder snapshot (tickers the screen covers only)."""
    try:
        info = yf.Ticker(symbol).info or {}
        found = {k: info[k] for k in STREET_KEYS if info.get(k) is not None}
        if isinstance(found.get("targetMeanPrice"), (int, float)):
            return found, "Yahoo Finance"
    except Exception as exc:
        log.warning("Yahoo Street targets failed for %s: %s", symbol, exc)
    snap = _snapshot()
    for rows in (snap.get("sectors") or {}).values():
        for row in rows:
            if str(row.get("ticker", "")).upper() != symbol.upper():
                continue
            t = row.get("targets") or {}
            if not isinstance(t.get("mean"), (int, float)):
                break
            found = {"targetMeanPrice": t.get("mean"), "targetLowPrice": t.get("low"),
                     "targetHighPrice": t.get("high"),
                     "numberOfAnalystOpinions": row.get("n_analysts"),
                     "recommendationMean": row.get("rating_mean"),
                     "recommendationKey": row.get("rating_key")}
            return ({k: v for k, v in found.items() if v is not None},
                    f"Stock Finder snapshot {snap.get('date', '')}".strip())
    return {}, ""


def _quote_summary(symbol: str) -> dict:
    """ticker.info, asked again after a pause when Yahoo refuses it."""
    info: dict = {}
    for attempt, pause in enumerate([0.0] + config.QUOTE_RETRY_DELAYS_S):
        if pause:
            time.sleep(pause)
        try:
            # A fresh Ticker each time: yfinance keeps the first answer, refused or not.
            info = yf.Ticker(symbol).info or {}
        except Exception as exc:
            log.warning("Yahoo quote summary failed for %s: %s", symbol, exc)
            info = {}
        if info.get("currentPrice") or info.get("regularMarketPrice"):
            if attempt:
                log.info("Yahoo quote summary for %s answered on try %d", symbol, attempt + 1)
            return info
    return info


def fetch(query: str) -> MarketData:
    """Resolve the query and assemble the MARKET DATA block for the prompt."""
    symbol = resolve_symbol(query)
    ticker = yf.Ticker(symbol)
    warnings: list[str] = []

    info = _quote_summary(symbol)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    refused = price is None
    if price is None:
        # Yahoo can refuse a datacenter IP's quote-summary call, which yfinance swallows,
        # leaving an info dict with no price: TSM came back like that from Render while
        # quoting normally from a home connection. The chart endpoint is throttled
        # separately, so try it before calling the symbol dead.
        log.warning("No price in Yahoo quote summary for %s (got keys: %s) — trying chart data",
                    symbol, ", ".join(sorted(info)) or "none")
        chart = _chart_snapshot(ticker)
        price = chart.pop("price", None)
        if price is None:
            raise TickerNotFound(
                f"Could not get a price for {symbol}. It may be delisted, or Yahoo Finance "
                "may be limiting this server — try again in a few minutes."
            )
        warnings.append("Yahoo's company summary was unavailable: price, market cap and share "
                        "count come from chart data, and valuation ratios and margins are missing")
        info = {**chart, **info}

    def _statement(attr: str):
        try:
            frame = getattr(ticker, attr)
        except Exception:
            warnings.append(f"{attr} unavailable")
            return None
        return operating_ebitda(frame) if "income" in attr else frame

    sections: list[str] = []
    sections.append(f"# MARKET DATA — {info.get('longName', symbol)} ({symbol})")
    sections.append(f"Pulled from Yahoo Finance on {date.today().isoformat()}. "
                    "These are the authoritative reported figures for this pitch.")

    # Resolve the statement currency and its trading-currency conversion before exposing
    # any provider valuation multiple. Yahoo's EV may be in the listing currency while its
    # EBITDA is in the reporting currency (NVO: USD/DKK); the raw ratio is then unusable.
    currency = info.get("currency") or "USD"
    reporting = info.get("financialCurrency")
    if not reporting:
        import sec_xbrl
        reporting = sec_xbrl.reporting_currency(symbol)
        if reporting and reporting != MINOR_UNITS.get(currency, currency):
            log.info("%s reports in %s (SEC filings); Yahoo's summary gave none", symbol,
                     reporting)
        elif not reporting and refused:
            warnings.append(f"the reporting currency is unknown (Yahoo's summary was refused "
                            f"and the SEC has no filings): statements are taken as {currency}")
    reporting = reporting or currency
    fx = _fx_rate(reporting, currency)
    if fx is None:
        warnings.append(f"no {reporting}->{currency} exchange rate; per-share value assumes 1.0")
        fx = 1.0
    fx_date = date.today().isoformat()

    snapshot = ["## Snapshot", "", "| Field | Value |", "| --- | --- |",
                f"| Current price | {price:,.2f} {info.get('currency', '')} |"]
    for key, label in INFO_FIELDS:
        if key in info and info[key] not in (None, ""):
            value = info[key]
            if (key in MONETARY_RATIOS and isinstance(value, (int, float)) and
                    MINOR_UNITS.get(reporting, reporting).upper() !=
                    MINOR_UNITS.get(currency, currency).upper()):
                value = float(value) / fx
            snapshot.append(f"| {label} | {_fmt_info_value(key, value)} |")
    sections.append("\n".join(snapshot))

    summary = info.get("longBusinessSummary")
    if summary:
        sections.append("## Business description (provider summary)\n\n" + summary.strip())

    income, balance = _statement("income_stmt"), _statement("balance_sheet")
    quarterly_balance = _statement("quarterly_balance_sheet")

    # A bank or insurer is valued on its book and earnings (valuation_equity.py), so its
    # block carries the lines that build them; other companies' blocks are unchanged.
    import valuation_route
    financial = bool(valuation_route.EQUITY_METHOD_INDUSTRY.search(info.get("industry") or ""))
    sections.append("## Annual financials\n")
    sections.append(_statement_table(income, INCOME_ROWS + (FINANCIAL_INCOME_ROWS if financial
                                                            else []), "Income statement"))
    sections.append(_statement_table(balance, BALANCE_ROWS + (FINANCIAL_BALANCE_ROWS if financial
                                                              else []), "Balance sheet"))
    sections.append(_statement_table(_statement("cashflow"), CASHFLOW_ROWS + (
        FINANCIAL_CASHFLOW_ROWS if financial else []), "Cash flow statement"))
    # A cyclical is judged against its whole cycle, which four years do not show.
    if valuation_route.CYCLICAL_INDUSTRY.search(info.get("industry") or ""):
        import sec_xbrl
        history = sec_xbrl.margin_history(symbol)
        if len(history) > 4:
            sections.append(cycle_table(history))

    quarterly_income = _statement("quarterly_income_stmt")
    sections.append("## Latest quarterly income statement\n")
    sections.append(_statement_table(quarterly_income, INCOME_ROWS,
                                     "Quarterly income statement", periods=4))

    # The valuation bridge, from the most recent balance sheet there is. These are the
    # numbers the code values the equity with, so the model sees exactly the same ones.
    cash_row, debt_row = BALANCE_ROWS[0], "Total Debt"
    cash = _latest(quarterly_balance, cash_row)
    debt = _latest(quarterly_balance, debt_row)
    debt_sheet = quarterly_balance
    if cash is None and debt is None:
        cash, debt = _latest(balance, cash_row), _latest(balance, debt_row)
        debt_sheet = balance
    leases = operating_leases(symbol, debt_sheet) if debt else 0.0
    if leases:
        debt = max(debt - leases, 0.0)
    known = cash is not None or debt is not None
    minority = _latest(quarterly_balance, "Minority Interest")
    if minority is None:
        minority = _latest(balance, "Minority Interest")
    # Stakes in associates and joint ventures, at book: the mirror of minority interests,
    # owned by the shareholders but outside the cash flows the DCF values.
    investments = _latest(quarterly_balance, "Long Term Equity Investment")
    if investments is None:
        investments = _latest(balance, "Long Term Equity Investment")
    if not known:
        warnings.append("cash and debt unavailable; the valuation bridge takes them as zero")
    revenue = _latest(income, "Total Revenue")
    ttm = _trailing_sum(quarterly_income, "Total Revenue")
    ttm_ebitda = _trailing_sum(quarterly_income, "EBITDA")
    ttm_net_income = _trailing_sum(quarterly_income, "Net Income")
    ttm_operating = _trailing_sum(quarterly_income, "Operating Income")
    ttm_pretax = _trailing_sum(quarterly_income, "Pretax Income")
    ttm_tax = _trailing_sum(quarterly_income, "Tax Provision")
    gap_note = net_income_gap(ttm_ebitda, ttm_net_income, ttm_operating, ttm_pretax, ttm_tax)
    if gap_note:
        warnings.append(gap_note)
    # The prompt shows the count the valuation will use; MarketData keeps the provider's,
    # so a replay records what Yahoo said and valuation.compute makes the same correction.
    raw_shares = info.get("sharesOutstanding")
    shares, share_note = share_count(raw_shares, info.get("marketCap"), price)
    if share_note:
        warnings.append(share_note)

    beta, beta_source = chart_beta(symbol), "5-year monthly vs the S&P 500"
    if beta is None and isinstance(info.get("beta"), (int, float)):
        beta, beta_source = float(info["beta"]), "Yahoo Finance"
    if beta is None:
        beta_source = ""
        warnings.append(f"no beta: the cost of capital assumes {config.DEFAULT_BETA:.1f}")
    risk_free, risk_free_source = treasury_yield(), "10-year Treasury"
    home_ccy = MINOR_UNITS.get(reporting, reporting)
    if home_ccy != "USD":
        home = home_yield(home_ccy)
        if home:
            risk_free, risk_free_source = home
        else:
            warnings.append(f"no {home_ccy} government yield: the discount rate uses the US "
                            "Treasury on a forecast in another currency")

    bridge = ["## Valuation inputs used by the model", "",
              "Your DCF is computed in code from your assumptions and these figures.", "",
              "| Item | Value |", "| --- | --- |",
              f"| Reporting currency | {reporting} |",
              f"| Trading currency | {currency} |"]
    if reporting != currency:
        bridge.append(f"| FX ({currency} per {reporting}, {fx_date}) | {fx:,.4f} |")
    bridge += [f"| Latest annual revenue ({reporting} mn) | "
               f"{f'{revenue:,.0f}' if revenue is not None else 'n.a.'} |",
               f"| Trailing four quarters' revenue ({reporting} mn) | "
               f"{f'{ttm:,.0f}' if ttm is not None else 'n.a.'} |",
               f"| Cash and short-term investments ({reporting} mn) | "
               f"{f'{cash:,.0f}' if cash is not None else 'n.a.'} |",
               f"| Total debt ({reporting} mn) | {f'{debt:,.0f}' if debt is not None else 'n.a.'} |",
               *([f"| Operating leases left out of debt ({reporting} mn) | {leases:,.0f}: "
                  "US GAAP, their cost is already in operating income |"] if leases else []),
               *([f"| Minority interests ({reporting} mn) | {minority:,.0f} |"] if minority else []),
               *([f"| Equity-method and other long-term equity investments, at book "
                  f"({reporting} mn) | {investments:,.0f} |"] if investments else []),
               f"| Shares outstanding (mn) | {f'{shares / 1e6:,.1f}' if shares else 'n.a.'} |",
               f"| Beta ({beta_source or 'unavailable'}) | "
               f"{f'{beta:.2f}' if beta is not None else 'n.a.'} |",
               f"| Risk-free rate ({risk_free_source}) | "
               f"{f'{risk_free:.2%}' if risk_free is not None else 'n.a.'} |"]
    # Today's multiple on these figures, which is what {{ev_ebitda}} cites. Yahoo's own
    # EV/EBITDA in the snapshot is on another basis: for NEE on 2026-09-27 it was 18.98x
    # (its EV includes minority interests, its EBITDA is its own) against 15.0x here, and
    # a pitch quoting one was marked wrong by a reviewer reading the other.
    if ttm_ebitda and ttm_ebitda > 0 and shares and price:
        ev = (float(price) * shares / fx / 1e6 + (debt or 0.0) - (cash or 0.0)
              + (minority or 0.0))
        bridge += [f"| Trailing four quarters' EBITDA ({reporting} mn) | {ttm_ebitda:,.0f} |",
                   f"| EV/EBITDA on these figures (market cap + debt - cash"
                   f"{' + minority interests' if minority else ''}, over trailing "
                   f"EBITDA) | {ev / ttm_ebitda:.1f}x |"]
        yahoo = info.get("enterpriseToEbitda")
        if (isinstance(yahoo, (int, float)) and
                reporting.upper() != MINOR_UNITS.get(currency, currency).upper()):
            yahoo = float(yahoo) / fx
        if isinstance(yahoo, (int, float)) and yahoo > 0 and abs(yahoo / (ev / ttm_ebitda) - 1) > 0.1:
            bridge.append(f"| Note | Yahoo's EV/EBITDA, currency-adjusted to {reporting}, is "
                          f"{yahoo:.2f}x; its own EV and EBITDA basis differs from the code "
                          "and peer multiples above. Say which basis you quote |")
    sections.append("\n".join(bridge))

    if warnings:
        sections.append("## Data gaps\n\n" + "\n".join(f"- {w}" for w in warnings))

    street_source = "Yahoo Finance" if isinstance(info.get("targetMeanPrice"), (int, float)) else ""
    if not street_source:
        street, street_source = street_targets(symbol)
        info = {**info, **street}
        if street_source:
            log.info("Street targets for %s from %s", symbol, street_source)
    target = info.get("targetMeanPrice")
    firms = firm_targets(ticker, config.STREET_FIRMS) if config.STREET_FIRMS else []
    quoted_multiple = (float(info["enterpriseToEbitda"])
                       if isinstance(info.get("enterpriseToEbitda"), (int, float)) else None)
    if (quoted_multiple is not None and
            reporting.upper() != MINOR_UNITS.get(currency, currency).upper()):
        # EV is quoted in trading currency; EBITDA is in reporting currency. Convert the
        # denominator to trading currency, so divide the raw ratio by trading/reporting FX.
        quoted_multiple /= fx
    return MarketData(
        symbol=symbol,
        name=info.get("longName") or info.get("shortName") or symbol,
        exchange=info.get("fullExchangeName") or info.get("exchange") or "",
        currency=currency,
        price=float(price),
        market_cap=info.get("marketCap"),
        shares_outstanding=raw_shares,
        prompt_block="\n\n".join(sections),
        warnings=warnings,
        reporting_currency=reporting,
        fx=fx,
        fx_date=fx_date,
        cash=cash or 0.0,
        debt=debt or 0.0,
        cash_debt_known=known,
        minority_interest=minority or 0.0,
        equity_investments=investments or 0.0,
        operating_leases=leases,
        revenue=revenue,
        ttm_revenue=ttm,
        ttm_ebitda=ttm_ebitda,
        ttm_net_income=ttm_net_income,
        ttm_operating_income=ttm_operating,
        ttm_pretax_income=ttm_pretax,
        ttm_tax=ttm_tax,
        quoted_ev_ebitda=quoted_multiple,
        analyst_target=float(target) if isinstance(target, (int, float)) else None,
        analyst_target_low=(float(info["targetLowPrice"])
                            if isinstance(info.get("targetLowPrice"), (int, float)) else None),
        analyst_target_high=(float(info["targetHighPrice"])
                             if isinstance(info.get("targetHighPrice"), (int, float)) else None),
        analyst_rating=info.get("recommendationKey"),
        analyst_count=info.get("numberOfAnalystOpinions"),
        analyst_rating_mean=(float(info["recommendationMean"])
                             if isinstance(info.get("recommendationMean"), (int, float)) else None),
        street_source=street_source,
        firm_targets=firms,
        summary_refused=refused,
        beta=beta,
        beta_source=beta_source,
        risk_free=risk_free, risk_free_source=risk_free_source,
        history=_history(income),
    )


REPORTED_ROWS = {"Total Revenue": "revenue", "Operating Income": "ebit", "EBITDA": "ebitda",
                 "Free Cash Flow": "fcf", "Capital Expenditure": "capex",
                 "Cost Of Revenue": "cogs", "Gross Profit": "gross_profit",
                 "Depreciation": "depreciation", "Amortization Of Intangibles": "amortization"}


CYCLE_TITLE = "### Operating margin history"


def cycle_table(history: list[tuple[str, float, float]]) -> str:
    """Revenue, operating income and margin by fiscal year, from SEC XBRL, newest first."""
    lines = [f"{CYCLE_TITLE} (SEC XBRL, millions of reporting currency)", "",
             "| Fiscal year end | Revenue | Operating income | Operating margin |",
             "| --- | --- | --- | --- |"]
    for end, revenue, income in history:
        lines.append(f"| {end} | {revenue / 1e6:,.0f} | {income / 1e6:,.0f} | "
                     f"{income / revenue:.1%} |")
    return "\n".join(lines) + "\n"


def cycle_margins(prompt_block: str) -> list[tuple[str, float]]:
    """(fiscal year, operating margin), newest first: the SEC history when the block has it,
    else the annual income statement."""
    at = prompt_block.find(CYCLE_TITLE)
    if at >= 0:
        out = []
        for line in prompt_block[at:].split("\n")[4:]:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            try:
                out.append((f"FY{cells[0][:4]}", float(cells[3].rstrip("%")) / 100))
            except (ValueError, IndexError):
                continue
        if out:
            return out
    return [(y["label"], y["ebit"] / y["revenue"]) for y in reported_years(prompt_block)
            if y.get("ebit") is not None and y.get("revenue")]


def reported_years(prompt_block: str) -> list[dict]:
    """The annual statements in the market data block, newest first: revenue, cost of
    revenue, gross profit, operating income, EBITDA, free cash flow and capex by fiscal
    year, in millions. Read from the
    block the writer saw, so a saved replay gives the same answer as a live run."""
    years: dict[str, dict] = {}
    for title in ("### Income statement", "### Cash flow statement"):
        at = prompt_block.find(title)
        if at < 0:
            continue
        lines = prompt_block[at:].split("\n")
        header = next((l for l in lines if l.startswith("| Line item")), None)
        if header is None:
            continue
        dates = [c.strip() for c in header.strip().strip("|").split("|")[1:]]
        start = lines.index(header) + 2
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            key = REPORTED_ROWS.get(cells[0])
            if not key:
                continue
            for date_, cell in zip(dates, cells[1:]):
                try:
                    value = float(cell.replace(",", ""))
                except ValueError:
                    continue
                label = f"FY{date_[:4]}" if date_[:4].isdigit() else date_
                years.setdefault(label, {"label": label})[key] = value
    return sorted(years.values(), key=lambda y: y["label"], reverse=True)


def set_cell(prompt_block: str, title: str, row: str, column: int, value: float) -> str:
    """The block with one value of a statement table replaced, in millions."""
    at = prompt_block.find(title)
    if at < 0:
        return prompt_block
    lines = prompt_block[at:].split("\n")
    header = next((i for i, l in enumerate(lines) if l.startswith("| Line item")), None)
    if header is None:
        return prompt_block
    for i in range(header + 2, len(lines)):
        if not lines[i].startswith("|"):
            break
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if cells[0] == row and column + 1 < len(cells):
            cells[column + 1] = f"{value:,.0f}"
            lines[i] = "| " + " | ".join(cells) + " |"
            break
    return prompt_block[:at] + "\n".join(lines)


def statement_rows(prompt_block: str, title: str) -> tuple[list[str], dict[str, list]]:
    """A statement table of the market data block: its column dates, newest first, and
    each row's values (None where blank), in millions."""
    at = prompt_block.find(title)
    if at < 0:
        return [], {}
    lines = prompt_block[at:].split("\n")
    header = next((l for l in lines if l.startswith("| Line item")), None)
    if header is None:
        return [], {}
    dates = [c.strip() for c in header.strip().strip("|").split("|")[1:]]
    rows: dict[str, list] = {}
    for line in lines[lines.index(header) + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        values = []
        for cell in cells[1:]:
            try:
                values.append(float(cell.replace(",", "")))
            except ValueError:
                values.append(None)
        rows[cells[0]] = values
    return dates, rows


BALANCE_KEYS = {"Cash Cash Equivalents And Short Term Investments": "cash", "Total Debt": "debt",
                "Stockholders Equity": "equity", "Minority Interest": "minority",
                "Goodwill And Other Intangible Assets": "goodwill", "Net PPE": "ppe"}


def balance_years(prompt_block: str) -> dict[str, dict]:
    """The annual balance sheets in the market data block by fiscal year ("FY2025"): cash,
    debt, equity and goodwill with other intangibles, in millions, where given."""
    dates, rows = statement_rows(prompt_block, "### Balance sheet")
    years: dict[str, dict] = {}
    for row, key in BALANCE_KEYS.items():
        for date_, value in zip(dates, rows.get(row, [])):
            if value is not None and date_[:4].isdigit():
                years.setdefault(f"FY{date_[:4]}", {})[key] = value
    return years


def trailing_quarters(prompt_block: str, row: str, quarters: int = 4) -> float | None:
    """A row of the quarterly income statement summed over the latest four quarters."""
    _, rows = statement_rows(prompt_block, "### Quarterly income statement")
    values = rows.get(row, [])[:quarters]
    if len(values) < quarters or any(v is None for v in values):
        return None
    return sum(values)


# How far net income may sit from pre-tax income less tax before the statement is taken
# not to reconcile (a minority share, rounding).
NET_INCOME_RECONCILE = 0.05


def net_income_gap(ebitda, income, operating, pretax, tax) -> str | None:
    """Why trailing net income is above trailing EBITDA, when the statement says why.

    EBITDA here is operating income plus D&A, so net income above it has to come from below
    operating income: gains on investments, interest income, a tax credit. SPOT's four
    quarters to 2Q26 held EUR 852mn of those and paid EUR 166mn of tax, net of EUR 225mn of
    credits, so net income of EUR 3,339mn stood above EBITDA of EUR 2,757mn. When net income
    is pre-tax income less tax, and the items below operating income cover the gap, the
    figures are not misread: the note says so. None when there is no gap, or no such
    reconciliation (then data_gates withholds the rating)."""
    if None in (ebitda, income, operating, pretax, tax) or income <= 0 or ebitda > income:
        return None
    if abs(pretax - tax - income) > NET_INCOME_RECONCILE * abs(income):
        return None
    below = pretax - operating
    if below - tax < income - ebitda:
        return None
    return (f"trailing net income ({income:,.0f}mn) is above trailing EBITDA "
            f"({ebitda:,.0f}mn): pre-tax income carries {below:,.0f}mn from below operating "
            f"income (investment gains, interest, other income) and tax is {tax:,.0f}mn. "
            "Value the operating business from operating income; do not capitalise those "
            "items as recurring")


def operating_ebitda(income):
    """The income statement with EBITDA as operating income plus D&A, where both are given.

    Yahoo's EBITDA is pre-tax income plus interest and D&A, so it carries what sits below
    operating income: NVDA's of 2026-10-12 held USD 31bn of gains on its equity stakes over
    four quarters. Its FY2026 "D&A" (EBITDA less operating income) came out at 6.6% of
    revenue against the 1.3% its cash flow statement adds back, the writer modelled 7%, and
    the trailing multiple read 23.5x where Yahoo's own is 27.3x."""
    if (income is None or getattr(income, "empty", True)
            or "Operating Income" not in income.index
            or "Reconciled Depreciation" not in income.index):
        return income
    built = income.loc["Operating Income"] + income.loc["Reconciled Depreciation"]
    out = income.copy()
    if "EBITDA" in out.index:
        out.loc["EBITDA"] = built.where(built.notna(), out.loc["EBITDA"])
    else:
        out.loc["EBITDA"] = built
    return out


def _history(income) -> list:
    """Revenue and EBITDA by reported year, oldest first, in millions."""
    if income is None or getattr(income, "empty", True) or "Total Revenue" not in income.index:
        return []

    def figure(row, col):
        if row not in income.index:
            return None
        raw = income.loc[row, col]
        return None if pd.isna(raw) else float(raw) / 1e6

    years = []
    for col in income.columns:
        revenue = figure("Total Revenue", col)
        if revenue is not None:
            label = f"FY{col.year}" if hasattr(col, "year") else str(col)
            years.append((label, revenue, figure("EBITDA", col)))
    return list(reversed(years))


@dataclass
class PeerMultiples:
    symbol: str
    name: str
    ev_ebitda: float | None
    pe: float | None
    # A listed stake's quote rather than a peer (stake_quote): its price in the valued
    # company's reporting currency, and the quote's own currency.
    price: float | None = None
    currency: str | None = None
    stake: bool = False


# Multiples outside these are data errors, not valuations: most often a peer whose
# statements are in one currency and whose price is in another (an ADR such as TSM, with
# TWD statements and a USD price, comes out ~30x too low). Dropped rather than converted,
# because the reporting currency is only in the quote summary Yahoo refuses on Render.
EV_EBITDA_RANGE = (2.0, 100.0)
PE_RANGE = (3.0, 200.0)
# Above this share of revenue, "D&A" holds a cost the business pays in cash to operate.
DA_SHARE_MAX = 0.3


def _peer(symbol: str) -> PeerMultiples | None:
    """One peer's EV/EBITDA and P/E, computed from its chart data and statements.

    Not from `ticker.info`: that is the quote summary Yahoo refuses from Render's IPs,
    so ready-made ratios would work at home and silently vanish in production. Market
    cap comes from the chart endpoint, the rest from the statements, the same sources the
    pitch's own figures use.
    """
    ticker = yf.Ticker(symbol)
    chart = _chart_snapshot(ticker)
    market_cap = chart.get("marketCap")
    if not market_cap:
        return None
    currency = chart.get("currency")
    if not currency:
        return None
    import sec_xbrl
    reporting = sec_xbrl.reporting_currency(symbol) or currency
    fx = _fx_rate(reporting, currency)
    if fx is None or fx <= 0:
        log.warning("peer %s currency conversion %s->%s unavailable; dropping its multiples",
                    symbol, reporting, currency)
        return None
    try:
        quarterly_income = operating_ebitda(ticker.quarterly_income_stmt)
        quarterly_balance = ticker.quarterly_balance_sheet
    except Exception as exc:
        log.warning("peer %s statements unavailable: %s", symbol, exc)
        return None
    ebitda = _trailing_sum(quarterly_income, "EBITDA")
    net_income = _trailing_sum(quarterly_income, "Net Income")
    cash = _latest(quarterly_balance, BALANCE_ROWS[0]) or 0.0
    debt = _latest(quarterly_balance, "Total Debt") or 0.0
    # On the same basis as the pitch's own bridge.
    debt = max(debt - operating_leases(symbol, quarterly_balance), 0.0) if debt else 0.0
    minority = _latest(quarterly_balance, "Minority Interest") or 0.0
    # Statements and debt are in reporting currency; translate market cap to that same
    # unit before computing EV/EBITDA or P/E.
    ev = market_cap / fx / 1e6 + debt - cash + minority
    market_cap_reporting = market_cap / fx

    revenue = _trailing_sum(quarterly_income, "Total Revenue")
    da = _trailing_sum(quarterly_income, "Reconciled Depreciation")
    ev_ebitda = ev / ebitda if ebitda and ebitda > 0 else None
    if ev_ebitda is not None and revenue and da and da > DA_SHARE_MAX * revenue:
        # Yahoo's D&A for NFLX (37% of revenue) and WBD (42%) includes content
        # amortization, a cost they pay in cash to operate: NFLX's EV/EBITDA came out at
        # 8.5x and set SPOT's peer median on 2026-10-11. Towers, telecoms and cable run
        # 14-19%. P/E is unaffected.
        log.info("peer %s EV/EBITDA dropped: D&A is %.0f%% of revenue", symbol, da / revenue * 100)
        ev_ebitda = None
    pe = market_cap_reporting / 1e6 / net_income if net_income and net_income > 0 else None
    if ev_ebitda is not None and not EV_EBITDA_RANGE[0] <= ev_ebitda <= EV_EBITDA_RANGE[1]:
        ev_ebitda = None
    if pe is not None and not PE_RANGE[0] <= pe <= PE_RANGE[1]:
        pe = None
    if ev_ebitda is None and pe is None:
        return None
    name = chart.get("longName") or chart.get("shortName") or symbol
    return PeerMultiples(symbol=symbol, name=str(name), ev_ebitda=ev_ebitda, pe=pe)


def stake_quote(symbol: str, reporting: str) -> PeerMultiples | None:
    """The share price of a listed holding, converted to the holder's reporting currency."""
    chart = _chart_snapshot(yf.Ticker(symbol))
    price, currency = chart.get("price"), chart.get("currency")
    if not price or not currency:
        return None
    # Pence and other minor units quote a hundredth of the major unit.
    major = MINOR_UNITS.get(currency, currency)
    if major != currency:
        price, currency = float(price) / 100, major
    fx = 1.0 if currency.upper() == reporting.upper() else _fx_rate(currency, reporting)
    if fx is None:
        return None
    return PeerMultiples(symbol=symbol, name=symbol, ev_ebitda=None, pe=None,
                         price=float(price) * fx, currency=currency, stake=True)


def peer_multiples(symbols: list[str]) -> list[PeerMultiples]:
    """Multiples for the peers that have usable data, in the order given."""
    from concurrent.futures import ThreadPoolExecutor

    cleaned = [str(s).strip().upper().lstrip("$") for s in symbols if str(s).strip()][:6]
    cleaned = list(dict.fromkeys(cleaned))

    def safe(symbol: str) -> PeerMultiples | None:
        try:
            return _peer(symbol)
        except Exception as exc:  # one bad peer must not sink the rest
            log.warning("peer %s skipped: %s", symbol, exc)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = list(pool.map(safe, cleaned))
    return [p for p in found if p is not None]
