"""Which valuation method a company gets, decided from data before the writer starts.

`fcff` is the enterprise DCF in `valuation.compute`; `equity` is a bank, insurer or other
lender valued on excess returns to equity (`valuation_equity.py`). `unsupported` is kept
for a method the code cannot run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import market_data
from market_data import MarketData

INDUSTRY_ROW = re.compile(r"^\| Industry \| (.+?) \|", re.M)
# Banks and insurers lend and invest what they borrow: deposits and policy liabilities are
# operating capital, not debt. Insurance brokers (Marsh, Aon) earn fees and are left out.
FINANCIAL_INDUSTRY = re.compile(r"\bBanks?\b|^Insurance\b(?! Brokers)|Mortgage Finance", re.I)
# Industries that hold both lenders and fee businesses: Goldman Sachs and Schwab sit in
# Capital Markets beside asset-light dealers, Capital One and American Express in Credit
# Services beside Visa and Mastercard. The balance sheet tells them apart.
MIXED_FINANCIAL_INDUSTRY = re.compile(r"Capital Markets|Credit Services|Financial Conglomerates",
                                      re.I)
# Yahoo's REIT industries all begin "REIT - "; regulated utilities "Utilities - Regulated".
# Independent power producers and renewables developers stay on the enterprise DCF: their
# returns are not set by a regulator.
REIT_INDUSTRY = re.compile(r"^REIT\b", re.I)
UTILITY_INDUSTRY = re.compile(r"^Utilities - Regulated", re.I)
# Industries valued on returns to equity, whose market data block carries book and earnings.
EQUITY_METHOD_INDUSTRY = re.compile("|".join(p.pattern for p in (
    FINANCIAL_INDUSTRY, MIXED_FINANCIAL_INDUSTRY, REIT_INDUSTRY, UTILITY_INDUSTRY)), re.I)
# Cyclical industries, judged against their whole cycle (valuation.cycle_check), and the
# commodity producers among them, whose revenue is a price deck.
COMMODITY_INDUSTRY = re.compile(
    r"Oil & Gas (E&P|Integrated|Drilling|Refining)|Coking Coal|Thermal Coal|Copper|Gold|"
    r"Silver|Steel|Aluminum|Uranium|Other (Industrial|Precious) Metals|Agricultural Inputs|"
    r"Lumber", re.I)
CYCLICAL_INDUSTRY = re.compile(
    COMMODITY_INDUSTRY.pattern + r"|Oil & Gas Equipment|Chemicals|Auto Manufacturers|"
    r"Auto Parts|Airlines|Farm & Heavy Construction Machinery|Specialty Industrial Machinery|"
    r"Residential Construction|Building Products|Semiconductor Equipment|Marine Shipping|"
    r"Trucking|"
    r"Railroads|Paper|Packaging", re.I)
# Total assets above this many times annual revenue is a balance sheet that lends: Capital
# One about 12x, Goldman Sachs about 30x, American Express about 4x; Visa about 2.6x,
# Mastercard about 1.7x, PayPal about 2.6x.
LENDER_ASSETS_TO_REVENUE = 3.5


@dataclass
class Route:
    method: str                                   # "fcff" | "equity" | "unsupported"
    industry: str = ""
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.method != "unsupported"


def industry(data: MarketData) -> str:
    found = INDUSTRY_ROW.search(getattr(data, "prompt_block", "") or "")
    return found.group(1) if found else ""


def _latest(rows: dict, name: str) -> float | None:
    return next((x for x in rows.get(name, []) if x is not None), None)


def assets_to_revenue(data: MarketData) -> float | None:
    """Latest annual total assets over latest annual revenue, from the market data block."""
    block = getattr(data, "prompt_block", "") or ""
    _, balance = market_data.statement_rows(block, "### Balance sheet")
    _, income = market_data.statement_rows(block, "### Income statement")
    assets, revenue = _latest(balance, "Total Assets"), _latest(income, "Total Revenue")
    if assets is None or not revenue or revenue <= 0:
        return None
    return assets / revenue


def _financial(name: str, why: str) -> Route:
    return Route("equity", name, ["financial"], [
        f"{name} is a financial institution{why}: its deposits, policy liabilities and "
        "loans are operating capital, so it is valued on returns to equity "
        "(excess returns over the cost of equity on book value), not by enterprise DCF"])


def route(data: MarketData) -> Route:
    name = industry(data)
    if name and REIT_INDUSTRY.search(name):
        return Route("equity", name, ["reit"], [
            f"{name}: a REIT pays out most of its cash and grows by investing with new debt "
            "and equity, so it is valued on AFFO left for shareholders after their share of "
            "that investment, cross-checked on net asset value"])
    if name and UTILITY_INDUSTRY.search(name):
        return Route("equity", name, ["utility"], [
            f"{name}: a regulated utility earns an allowed return on its rate base, so it is "
            "valued on excess returns to equity as its rate base grows, not by enterprise DCF"])
    if name and FINANCIAL_INDUSTRY.search(name):
        return _financial(name, "")
    if name and MIXED_FINANCIAL_INDUSTRY.search(name):
        ratio = assets_to_revenue(data)
        if ratio is None:
            return _financial(name, " (its balance sheet could not be read to show it does "
                                    "not lend)")
        if ratio > LENDER_ASSETS_TO_REVENUE:
            return _financial(name, f" (total assets {ratio:.1f}x revenue, a lending "
                                    "balance sheet)")
        return Route("fcff", name, [], [f"{name}, but total assets are only {ratio:.1f}x "
                                        "revenue: a fee business, valued by enterprise DCF"])
    return Route("fcff", name)
