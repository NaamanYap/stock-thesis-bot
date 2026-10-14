"""Market data and Yahoo's statements, as facts.

The price, share count, market value, beta, Treasury yield and the Street's target come
from Yahoo Finance (market_data.py). Yahoo's financial statements are the least trusted
source: they only fill a metric and period the company's own filings left empty, which
for a company that does not file with the SEC is most of them.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import yfinance as yf

from .facts_db import Fact, FactDB
from market_data import MarketData
from .periods import Period, fy_end_from_annual_ends

log = logging.getLogger(__name__)

# Yahoo's row name -> (metric, label, sign): sign -1 for outflows Yahoo prints negative.
INCOME = {
    "Total Revenue": ("revenue", "Revenue", 1),
    "Cost Of Revenue": ("cost_of_revenue", "Cost of revenue", 1),
    "Gross Profit": ("gross_profit", "Gross profit", 1),
    "Operating Income": ("operating_income", "Operating income", 1),
    "Pretax Income": ("pretax_income", "Income before taxes", 1),
    "Tax Provision": ("income_tax", "Income tax expense", 1),
    "Net Income Common Stockholders": ("net_income", "Net income attributable to shareholders", 1),
    "Diluted EPS": ("diluted_eps", "Diluted EPS", 1),
    "Diluted Average Shares": ("diluted_shares", "Diluted weighted average shares", 1),
    "Interest Expense": ("interest_expense", "Interest expense", 1),
    "Reconciled Depreciation": ("d_and_a", "Depreciation and amortization", 1),
    "Research And Development": ("rd_expense", "Research and development", 1),
    "Selling General And Administration": ("sga_expense", "Selling, general and administrative", 1),
}
BALANCE = {
    "Cash And Cash Equivalents": ("cash", "Cash and cash equivalents", 1),
    "Other Short Term Investments": ("short_term_investments", "Short-term investments", 1),
    "Current Debt": ("debt_current", "Debt due within a year", 1),
    "Long Term Debt": ("debt_noncurrent", "Long-term debt, non-current", 1),
    "Total Assets": ("total_assets", "Total assets", 1),
    "Current Assets": ("current_assets", "Current assets", 1),
    "Current Liabilities": ("current_liabilities", "Current liabilities", 1),
    "Stockholders Equity": ("equity", "Shareholders' equity", 1),
    "Minority Interest": ("minority_interest", "Minority interests", 1),
    "Goodwill": ("goodwill", "Goodwill", 1),
    "Other Intangible Assets": ("intangibles", "Intangible assets other than goodwill", 1),
    "Inventory": ("inventory", "Inventory", 1),
    "Accounts Receivable": ("receivables", "Accounts receivable", 1),
    "Accounts Payable": ("payables", "Accounts payable", 1),
}
CASH_FLOW = {
    "Operating Cash Flow": ("operating_cash_flow", "Operating cash flow", 1),
    "Capital Expenditure": ("capex", "Capital expenditure", -1),
    "Repurchase Of Capital Stock": ("buybacks", "Share repurchases", -1),
    "Cash Dividends Paid": ("dividends_paid", "Dividends paid", -1),
    "Stock Based Compensation": ("sbc", "Stock-based compensation", 1),
    "Purchase Of Business": ("acquisitions", "Acquisitions, net of cash acquired", -1),
}
STATEMENTS = [("income_stmt", "12M", INCOME), ("quarterly_income_stmt", "3M", INCOME),
              ("balance_sheet", "at", BALANCE), ("quarterly_balance_sheet", "at", BALANCE),
              ("cashflow", "12M", CASH_FLOW), ("quarterly_cashflow", "3M", CASH_FLOW)]


def market_facts(db: FactDB, data: MarketData) -> None:
    """Point-in-time market figures, dated today."""
    today = date.today().isoformat()
    now = Period("latest", None, db.fy_end)
    cur = data.currency

    def add(fid, label, value, unit, location="", source="Yahoo Finance"):
        if value is None:
            return None
        return db.add(Fact(id=fid, metric=fid, label=label, value=float(value), unit=unit,
                           period=now, data_type="market", source=f"{source}, {today}",
                           source_kind="market", source_date=today, source_location=location))

    price = add("share_price", "Share price", data.price, f"{cur}/share")
    mcap = add("market_cap", f"Market capitalisation ({cur})", data.market_cap, cur)
    add("shares_outstanding_market", "Shares outstanding (Yahoo)", data.shares_outstanding,
        "shares")
    if mcap is not None and data.reporting_currency != cur and data.fx:
        db.add(Fact(
            id="market_cap_reporting", metric="market_cap_reporting",
            label=f"Market capitalisation in {data.reporting_currency}",
            value=mcap.value / data.fx, unit=data.reporting_currency, period=now,
            data_type="calculated", source="calculated", source_kind="market",
            source_date=today, formula=f"market_cap / fx ({data.fx:.4f} {cur} per "
                                       f"{data.reporting_currency})", inputs=["market_cap"]))
    add("beta", f"Beta ({data.beta_source})" if data.beta_source else "Beta", data.beta, "pure")
    if data.risk_free is not None:
        add("risk_free_rate", "10-year US Treasury yield", data.risk_free * 100, "%")
    street = data.street_source or "Yahoo Finance"
    add("street_target_mean", "Street average price target", data.analyst_target,
        f"{cur}/share", source=street)
    add("street_target_low", "Street lowest price target", data.analyst_target_low,
        f"{cur}/share", source=street)
    add("street_target_high", "Street highest price target", data.analyst_target_high,
        f"{cur}/share", source=street)
    add("street_analysts", "Analysts covering", data.analyst_count, "count", source=street)
    add("street_rating_mean", "Street mean rating (1 strong buy to 5 sell)",
        data.analyst_rating_mean, "pure", source=street)
    if price is not None and data.analyst_target:
        db.add(Fact(
            id="street_upside", metric="street_upside", label="Street average target vs price",
            value=(data.analyst_target / data.price - 1) * 100, unit="%", period=now,
            data_type="calculated", source="calculated", source_kind="market",
            source_date=today, formula="street_target_mean / share_price - 1",
            inputs=["street_target_mean", "share_price"]))
    if data.reporting_currency != cur:
        add("fx_rate", f"{cur} per {data.reporting_currency}", data.fx, "pure")
    for w in data.warnings:
        db.gaps.append(f"market data: {w}")


def _frames(symbol: str) -> dict[str, pd.DataFrame]:
    ticker = yf.Ticker(symbol)
    out = {}
    for attr, _, _ in STATEMENTS:
        try:
            df = getattr(ticker, attr)
        except Exception as exc:
            log.warning("Yahoo %s unavailable for %s: %s", attr, symbol, exc)
            continue
        if isinstance(df, pd.DataFrame) and not df.empty:
            out[attr] = df
    return out


def yahoo_statements(db: FactDB, symbol: str, frames: dict[str, pd.DataFrame] | None = None,
                     set_fy_end: bool = False) -> int:
    """Fill the metrics and periods the filings did not give from Yahoo's statements.
    Returns how many facts were added."""
    frames = _frames(symbol) if frames is None else frames
    if set_fy_end and "income_stmt" in frames:
        ends = [pd.Timestamp(c).date() for c in frames["income_stmt"].columns]
        db.fy_end = fy_end_from_annual_ends(ends)
    today = date.today().isoformat()
    added = 0
    for attr, span, rows in STATEMENTS:
        df = frames.get(attr)
        if df is None:
            continue
        for row, (metric, label, sign) in rows.items():
            if row not in df.index:
                continue
            for col, value in df.loc[row].items():
                if value is None or pd.isna(value):
                    continue
                end = pd.Timestamp(col).date()
                period = Period(span, end, db.fy_end)
                if db.one(metric, period) is not None:
                    continue
                unit = ("shares" if metric == "diluted_shares" else
                        f"{db.currency}/share" if metric == "diluted_eps" else db.currency)
                v = float(value) * sign if sign < 0 else float(value)
                db.add(Fact(
                    id=db.make_id(metric, period), metric=metric, label=label, value=v,
                    unit=unit, period=period, data_type="reported",
                    source=f"Yahoo Finance statements, {today}", source_kind="market",
                    source_date=today, source_location=f"{attr}, row '{row}'"))
                added += 1
    return added


def price_history(symbol: str) -> tuple[pd.Series | None, list[tuple[date, float]]]:
    """Six years of month-end closes indexed by month, adjusted for splits but not for
    dividends (a dividend does not change what the company was worth that day), and the
    splits themselves as (date, ratio)."""
    try:
        ticker = yf.Ticker(symbol)
        closes = ticker.history(period="6y", interval="1mo", auto_adjust=False)["Close"].dropna()
        index = closes.index.tz_localize(None) if closes.index.tz is not None else closes.index
        closes.index = index.to_period("M")
        closes = closes[~closes.index.duplicated(keep="last")]
        splits = [(pd.Timestamp(d).date(), float(r)) for d, r in ticker.splits.items() if r]
        return closes, splits
    except Exception as exc:
        log.warning("price history unavailable for %s: %s", symbol, exc)
        return None, []
