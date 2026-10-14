"""Banks and insurers: equity valued from returns on book, as a sell-side analyst builds it.

The writer forecasts the drivers an analyst argues about: for a bank, earning assets and
the net interest margin, fee income, the efficiency ratio, loans and the credit cost; for an
insurer, net premiums, the combined ratio, invested assets and the investment yield. Code
builds net income from them, rolls book value forward with what is not paid out, and values
the equity as book today plus the discounted excess returns (ROE less the cost of equity
on opening book), with a terminal value at a sustainable ROE and growth. A dividend
discount on the same inputs must give the same value; that identity is asserted.

The result fills `valuation.Valuation` with `family = "equity"`, so every reader of a
valuation still works: each `YearRow` is a year of the bank (revenue = net revenue, EBIT =
pre-tax income, NOPAT = net income to common, FCF = distributions to shareholders), and
`wacc` is the cost of equity. The tables and placeholders here say what each figure is.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import config
import market_data
import valuation as val
from market_data import MarketData
from valuation import ModelInputError, Valuation, YearRow, _fraction, _number, _series

BANK_KEYS = ("earning_assets", "nim", "non_interest_income", "efficiency_ratio", "loans",
             "credit_cost")
INSURER_KEYS = ("net_premiums", "combined_ratio", "invested_assets", "investment_yield")
INSURER_OPTIONAL = ("other_income",)
# A regulated utility earns its allowed return on the equity share of its rate base; the
# holding company's own debt and unregulated businesses are `other_income`, after tax.
UTILITY_KEYS = ("rate_base", "equity_ratio", "earned_roe")
UTILITY_OPTIONAL = ("allowed_roe", "other_income")
# A REIT distributes most of its cash: AFFO is net operating income less overhead, interest
# and the capex that keeps the buildings let; growth comes from investment funded partly
# with debt, and what shareholders keep is AFFO less their share of that investment.
REIT_KEYS = ("noi", "g_and_a", "interest_expense", "maintenance_capex", "net_investment",
             "debt_funded", "acquisition_cap_rate")
REIT_OPTIONAL = ("other_income",)
KIND_KEYS = {"bank": (BANK_KEYS, ()), "insurer": (INSURER_KEYS, INSURER_OPTIONAL),
             "utility": (UTILITY_KEYS, UTILITY_OPTIONAL), "reit": (REIT_KEYS, REIT_OPTIONAL)}
MONEY_KEYS = {"earning_assets", "non_interest_income", "loans", "net_premiums",
              "invested_assets", "other_income", "rate_base", "noi", "g_and_a",
              "interest_expense", "maintenance_capex", "net_investment"}
# An earned ROE above the allowed by more than this needs the writer's argument.
EARNED_OVER_ALLOWED = 0.005
# A REIT's value this far from its net asset value needs the text to explain it.
NAV_GAP_NOTE = 0.25
# A first year this far above the latest earnings is an error, whatever the rationale.
FIRST_YEAR_HARD = 0.5
# An insurer's other income above this share of pre-tax income is probably revenue.
OTHER_INCOME_SHARE = 0.25
# CET1 this far above the requirement at the end of the forecast is surplus capital.
EXCESS_CET1 = 0.03
# Ranges a driver is held to before the model is sent back. Outside them the figure is
# almost always a misread unit (a margin of 2.5 meant as 2.5%) or a business this method
# does not describe.
DRIVER_RANGES = {"nim": (0.005, 0.06), "efficiency_ratio": (0.25, 0.90),
                 "credit_cost": (0.0, 0.04), "combined_ratio": (0.75, 1.15),
                 "investment_yield": (0.01, 0.08), "equity_ratio": (0.30, 0.65),
                 "earned_roe": (0.04, 0.15), "allowed_roe": (0.07, 0.13),
                 "debt_funded": (0.0, 1.0), "acquisition_cap_rate": (0.03, 0.10)}
PAYOUT_RANGE = (0.0, 1.5)
# A forecast ROE above the best reported by this much needs the writer's argument.
ROE_ABOVE_RECORD = 0.03
# Terminal ROE this far from the final forecast year's needs the writer's argument.
TERMINAL_ROE_GAP = 0.02
# A terminal ROE above the cost of equity by this much is a moat few banks keep for ever.
TERMINAL_SPREAD_MAX = 0.08
# The first forecast year's net interest income or net income this far from the last
# reported year needs a bridge in `first_year_rationale`.
FIRST_YEAR_JUMP = 0.15
FIRST_YEAR_NI_JUMP = 0.30
SCENARIO_KEYS = {"net_investment_change", "acquisition_cap_rate", "nim", "efficiency_ratio", "credit_cost", "combined_ratio",
                 "investment_yield", "payout", "terminal", "earning_assets_change",
                 "net_premiums_change", "cost_of_equity", "earned_roe", "equity_ratio",
                 "rate_base_change", "noi_change", "net_investment", "debt_funded"}
# Drivers a scenario may replace outright, by kind.
SCENARIO_REPLACE = ("nim", "efficiency_ratio", "credit_cost", "combined_ratio",
                    "investment_yield", "earned_roe", "equity_ratio", "net_investment",
                    "debt_funded", "acquisition_cap_rate")
# Level changes a scenario may make, and the drivers each scales.
SCENARIO_SCALE = {"earning_assets_change": ("earning_assets", "loans"),
                  "net_premiums_change": ("net_premiums",),
                  "rate_base_change": ("rate_base",),
                  "noi_change": ("noi",),
                  "net_investment_change": ("net_investment",)}


@dataclass
class EquityYear:
    label: str
    revenue: float                  # net revenue: NII + fees, or premiums + investment income
    pretax: float
    tax: float
    preferred: float
    net_income: float               # to common shareholders
    book_open: float
    roe: float                      # on opening common book
    payout: float
    distributions: float
    book_close: float
    tangible_close: float
    excess_return: float
    factor: float
    lines: dict = field(default_factory=dict)   # the build: nii, fees, opex, provisions...
    cet1_ratio: float | None = None

    @property
    def rote(self) -> float | None:
        tangible_open = self.tangible_close - (self.book_close - self.book_open)
        return self.net_income / tangible_open if tangible_open > 0 else None


@dataclass
class EquityModel:
    kind: str                       # "bank" | "insurer"
    years: list[EquityYear]
    book0: float                    # common equity, base year, reporting mn
    tangible0: float | None
    ke: float
    growth: float
    terminal_roe: float
    terminal_payout: float
    pv_excess: float
    terminal_excess: float          # at the end of the final year
    pv_terminal: float
    value: float                    # equity, reporting mn
    ddm_value: float
    inputs: dict                    # what `build` needs to rebuild with one input changed
    reported: list[dict] = field(default_factory=list)   # book, NI, ROE by reported year
    capital_note: str = ""
    cases: dict = field(default_factory=dict)            # "bull"/"bear" -> EquityModel
    # A REIT's net asset value: forward NOI at a market cap rate, less net debt and
    # preferred, plus other assets at book (reporting mn).
    nav: float | None = None
    cap_rate: float | None = None
    net_debt: float | None = None
    other_assets: float = 0.0


# --------------------------------------------------------------------------- data

def _latest(rows: dict, *names: str) -> float | None:
    for name in names:
        x = next((v for v in rows.get(name, []) if v is not None), None)
        if x is not None:
            return x
    return None


def reported(data: MarketData) -> list[dict]:
    """Common equity, net income to common and ROE on average equity, oldest first."""
    block = getattr(data, "prompt_block", "") or ""
    bdates, balance = market_data.statement_rows(block, "### Balance sheet")
    idates, income = market_data.statement_rows(block, "### Income statement")
    equity = balance.get("Common Stock Equity") or balance.get("Stockholders Equity") or []
    tangible = balance.get("Tangible Book Value") or []
    ni = income.get("Net Income Common Stockholders") or income.get("Net Income") or []
    nii = income.get("Net Interest Income") or []
    by_year: dict[str, dict] = {}
    for d, x in zip(bdates, equity):
        if x is not None and d[:4].isdigit():
            by_year.setdefault(d[:4], {})["book"] = x
    for d, x in zip(bdates, tangible):
        if x is not None and d[:4].isdigit():
            by_year.setdefault(d[:4], {})["tangible"] = x
    for d, x in zip(idates, ni):
        if x is not None and d[:4].isdigit():
            by_year.setdefault(d[:4], {})["net_income"] = x
    for d, x in zip(idates, nii):
        if x is not None and d[:4].isdigit():
            by_year.setdefault(d[:4], {})["nii"] = x
    out, prev = [], None
    for year in sorted(by_year):
        y = {"label": f"FY{year}", **by_year[year]}
        if prev and prev.get("book") and y.get("book") and y.get("net_income") is not None:
            y["roe"] = y["net_income"] / ((prev["book"] + y["book"]) / 2)
            if prev.get("tangible") and y.get("tangible"):
                y["rote"] = y["net_income"] / ((prev["tangible"] + y["tangible"]) / 2)
        out.append(y)
        prev = y
    return out


def base_book(data: MarketData) -> tuple[float, float | None]:
    """Common equity and tangible common equity on the latest balance sheet, reporting mn."""
    block = getattr(data, "prompt_block", "") or ""
    _, balance = market_data.statement_rows(block, "### Balance sheet")
    common = _latest(balance, "Common Stock Equity")
    if common is None:
        total = _latest(balance, "Stockholders Equity")
        if total is None:
            raise ModelInputError("the market data has no common equity on the balance sheet, "
                                  "so book value cannot be rolled forward")
        common = total - (_latest(balance, "Preferred Stock Equity") or 0.0)
    tangible = _latest(balance, "Tangible Book Value")
    if tangible is not None:
        # Yahoo's tangible book can include preferred stock; tangible common is never above
        # common equity.
        tangible = min(tangible, common)
    return common, tangible


def _latest_income(data: MarketData, *names: str) -> float | None:
    block = getattr(data, "prompt_block", "") or ""
    _, income = market_data.statement_rows(block, "### Income statement")
    return _latest(income, *names)


def _latest_balance(data: MarketData, *names: str) -> float | None:
    block = getattr(data, "prompt_block", "") or ""
    _, balance = market_data.statement_rows(block, "### Balance sheet")
    return _latest(balance, *names)


# --------------------------------------------------------------------------- the build

def _drivers(model: dict, n: int) -> tuple[str, dict[str, list[float]]]:
    given = [k for k in KIND_KEYS if isinstance(model.get(k), dict)]
    if len(given) != 1:
        raise ModelInputError(
            "this company is valued on returns to equity: give one of " + "; ".join(
                f"`model.{k}` (" + ", ".join(keys) + ")" for k, (keys, _) in KIND_KEYS.items()))
    kind = given[0]
    raw = model[kind]
    keys, optional = KIND_KEYS[kind]
    out = {}
    for key in keys:
        out[key] = _series(raw, key, n, fraction=key not in MONEY_KEYS)
    for key in optional:
        if key == "allowed_roe" and raw.get(key) is None:
            continue
        out[key] = _series(raw, key, n, fraction=key not in MONEY_KEYS, required=False,
                           default=0.0)
    return kind, out


# What the terminal return is, by kind: the ROE on book for ever, or for a REIT the return
# its new investment earns for shareholders.
TERMINAL_RETURN = {"bank": "ROE", "insurer": "ROE", "utility": "ROE",
                   "reit": "return on new investment"}


def build(inp: dict, ke: float, growth: float, terminal_roe: float) -> EquityModel:
    """Net income from the drivers, book rolled forward, value from excess returns.

    For a REIT `terminal_roe` is the return on new investment, and "net income" is AFFO."""
    kind, d, n = inp["kind"], inp["drivers"], len(inp["years"])
    book, tangible = inp["book0"], inp["tangible0"]
    intangibles = book - tangible if tangible is not None else None
    years: list[EquityYear] = []
    prev_assets = inp.get("assets0")
    cet1 = inp.get("cet1")      # {"capital", "rwa", "density"} or None
    capital = cet1["capital"] if cet1 else None
    acquired_before, new_debt_before = 0.0, 0.0     # a REIT's external growth so far
    for i, label in enumerate(inp["years"]):
        close = distributions = None
        if kind == "utility":
            base = inp["assets0"]
            rate_base = d["rate_base"][i]
            average = (rate_base + prev_assets) / 2 if prev_assets else rate_base
            regulated = average * d["equity_ratio"][i] * d["earned_roe"][i]
            other = d["other_income"][i]
            revenue = pretax = regulated + other
            lines = {"rate_base": rate_base, "equity_ratio": d["equity_ratio"][i],
                     "earned_roe": d["earned_roe"][i], "regulated_income": regulated,
                     "other_income": other}
            if "allowed_roe" in d:
                lines["allowed_roe"] = d["allowed_roe"][i]
            # Book grows with the equity share of the rate base: what earnings do not fund
            # is new equity, a negative distribution.
            close = (inp["book0"] + rate_base * d["equity_ratio"][i]
                     - base * d["equity_ratio"][0])
            prev_assets = rate_base
        elif kind == "reit":
            # External growth as a REIT analyst models it: new investment earns its cap
            # rate from mid-year, and the debt-funded share pays the cost of new debt. Cut
            # the investment and its NOI goes with it (O's bear case of 2026-10-12 cut
            # acquisitions, kept their NOI and came out above the base case).
            invest, cap = d["net_investment"][i], d["acquisition_cap_rate"][i]
            debt_share = d["debt_funded"][i]
            acquired = acquired_before + 0.5 * invest * cap
            new_interest = (new_debt_before + 0.5 * invest * debt_share) * inp["new_debt_rate"]
            acquired_before += invest * cap
            new_debt_before += invest * debt_share
            existing, other = d["noi"][i], d["other_income"][i]
            noi = existing + acquired
            interest = d["interest_expense"][i] + new_interest
            affo = noi + other - d["g_and_a"][i] - interest - d["maintenance_capex"][i]
            equity_funded = invest * (1 - debt_share)
            revenue, pretax = noi + other, affo
            lines = {"existing_noi": existing, "acquired_noi": acquired, "noi": noi,
                     "other_income": other, "g_and_a": d["g_and_a"][i],
                     "interest_expense": interest, "new_debt_interest": new_interest,
                     "maintenance_capex": d["maintenance_capex"][i], "affo": affo,
                     "net_investment": invest, "acquisition_cap_rate": cap,
                     "debt_funded": debt_share, "equity_funded": equity_funded}
            distributions = affo - equity_funded
        elif kind == "bank":
            assets = d["earning_assets"][i]
            average = (assets + prev_assets) / 2 if prev_assets else assets
            nii = average * d["nim"][i]
            fees = d["non_interest_income"][i]
            revenue = nii + fees
            opex = revenue * d["efficiency_ratio"][i]
            provisions = d["loans"][i] * d["credit_cost"][i]
            pretax = revenue - opex - provisions
            lines = {"earning_assets": assets, "nim": d["nim"][i], "nii": nii, "non_interest_income": fees,
                     "efficiency_ratio": d["efficiency_ratio"][i], "opex": opex,
                     "loans": d["loans"][i], "credit_cost": d["credit_cost"][i],
                     "provisions": provisions}
            prev_assets = assets
        else:
            premiums = d["net_premiums"][i]
            underwriting = premiums * (1 - d["combined_ratio"][i])
            assets = d["invested_assets"][i]
            average = (assets + prev_assets) / 2 if prev_assets else assets
            investment = average * d["investment_yield"][i]
            other = d["other_income"][i]
            revenue = premiums + investment + other
            pretax = underwriting + investment + other
            lines = {"net_premiums": premiums, "combined_ratio": d["combined_ratio"][i],
                     "underwriting": underwriting, "invested_assets": assets,
                     "investment_yield": d["investment_yield"][i],
                     "investment_income": investment, "other_income": other}
            prev_assets = assets
        # A utility's earned ROE and a REIT's AFFO are after tax already.
        tax = pretax * inp["tax"][i] if pretax > 0 and kind in ("bank", "insurer") else 0.0
        preferred = inp["preferred"]
        ni = pretax - tax - preferred
        roe = ni / book if book > 0 else 0.0
        if close is not None:                       # utility: book follows the rate base
            distributions = ni - (close - book)
        elif distributions is None:                 # bank, insurer: a payout of earnings
            distributions = ni * inp["payout"][i]
        if close is None:
            close = book + ni - distributions
        payout = distributions / ni if ni else 0.0
        factor = 1 / (1 + ke) ** (i + 1)
        row = EquityYear(str(label), revenue, pretax, tax, preferred, ni, book, roe, payout,
                         distributions, close,
                         close - intangibles if intangibles is not None else close,
                         (roe - ke) * book, factor, lines)
        if cet1:
            rwa = assets * cet1["density"]
            capital += ni - distributions
            row.cet1_ratio = capital / rwa if rwa > 0 else None
        years.append(row)
        book = close
    if ke - growth < val.MIN_WACC_GROWTH_SPREAD:
        raise ModelInputError(f"terminal growth {growth:.1%} must sit at least 1pp below the "
                              f"{ke:.1%} cost of equity")
    if terminal_roe <= growth:
        raise ModelInputError(f"terminal {TERMINAL_RETURN[kind]} {terminal_roe:.1%} must be "
                              f"above terminal growth {growth:.1%}: below it the business "
                              "cannot fund its own growth")
    pv_excess = sum(y.excess_return * y.factor for y in years)
    last = years[-1]
    # Terminal payout is what growth leaves: growing at g, a business retains g / return of
    # its earnings. A bank's earnings after the forecast are its ROE on closing book; a
    # REIT's are its final AFFO grown a year.
    terminal_payout = 1 - growth / terminal_roe
    next_earnings = (last.net_income * (1 + growth) if kind == "reit"
                     else terminal_roe * last.book_close)
    next_dividend = next_earnings * terminal_payout
    terminal_dividends = next_dividend / (ke - growth)
    # Excess returns after the forecast, valued at its end: what the dividends are worth
    # beyond the book they leave. For a bank, (ROE - ke) x book / (ke - g).
    terminal_excess = terminal_dividends - last.book_close
    pv_terminal = terminal_excess * last.factor
    value = inp["book0"] + pv_excess + pv_terminal
    # The same inputs as a dividend discount must give the same value under clean surplus.
    ddm = (sum(y.distributions * y.factor for y in years)
           + terminal_dividends * last.factor
           + (last.book_close - inp["book0"] - sum(y.net_income - y.distributions
                                                     for y in years)) * last.factor)
    # The last term is zero under clean surplus; it is there so a broken roll-forward shows.
    assert abs(ddm - value) <= 1e-6 * max(1.0, abs(value)), (ddm, value)
    return EquityModel(kind, years, inp["book0"], inp["tangible0"], ke, growth, terminal_roe,
                       terminal_payout, pv_excess, terminal_excess, pv_terminal, value, ddm,
                       inp)


# --------------------------------------------------------------------------- compute

def cost_of_equity(model: dict, cost) -> float:
    if cost is not None:
        return cost.cost_of_equity + cost.premium
    if model.get("cost_of_equity") is None:
        raise ModelInputError("`model.cost_of_equity` is missing: with no house rate, give the "
                              "cost of equity and derive it in `wacc_rationale`")
    return _fraction(model["cost_of_equity"], "model.cost_of_equity")


def _capital(model: dict, data: MarketData, kind: str,
             assets0: float | None) -> tuple[dict | None, str, list[str]]:
    """`model.capital`: the CET1 ratio, risk-weighted assets and the requirement, sourced."""
    raw = model.get("capital")
    if kind != "bank":
        return None, ("Regulatory capital (solvency ratio) is not modelled; the book-value "
                      "roll-forward is the capital check."), []
    if not isinstance(raw, dict):
        return None, ("CET1 is not checked: the model gives no `capital` (CET1 ratio, "
                      "risk-weighted assets and the requirement from the latest filing)."), [
            "`model.capital` is missing: give `cet1_ratio`, `rwa` (reporting mn) and "
            "`requirement` from the latest filing, with `source`, so the code can check the "
            "payout leaves the bank above its requirement"]
    try:
        ratio = _fraction(raw.get("cet1_ratio"), "model.capital.cet1_ratio")
        rwa = _number(raw.get("rwa"), "model.capital.rwa")
        requirement = _fraction(raw.get("requirement"), "model.capital.requirement")
    except ModelInputError as exc:
        return None, "CET1 is not checked: " + str(exc), [str(exc)]
    warnings = []
    if not str(raw.get("source") or "").strip():
        warnings.append("`model.capital` needs a `source` naming the filing its CET1 figures "
                        "come from")
    # Risk weight per unit of earning assets, held at the base year's: RWA grows with the
    # balance sheet. Without `base_earning_assets` the base is total assets, which makes the
    # density lower and the check stricter.
    base = assets0 or _latest_balance(data, "Total Assets")
    if not base:
        return None, "CET1 is not checked: no base-year earning assets.", warnings
    density = rwa / base
    return ({"capital": ratio * rwa, "rwa": rwa, "density": density,
             "requirement": requirement},
            f"CET1 rolls forward from {ratio:.1%} with retained earnings; risk-weighted assets "
            f"grow with earning assets at {density:.0%} of them (the base year's density).",
            warnings)


def nav_inputs(em: EquityModel, model: dict, data: MarketData, warnings: list[str]) -> None:
    """`model.reit.cap_rate`: the rate the private market pays for these buildings, sourced
    (a broker survey or a recent comparable sale). NAV is the first forecast year's NOI at
    that rate, less net debt and preferred, plus `other_assets` (land, development at cost)."""
    raw = model.get("reit") or {}
    em.net_debt = (data.debt or 0.0) - (data.cash or 0.0)
    if raw.get("cap_rate") is None:
        warnings.append("`model.reit.cap_rate` is missing: give the market cap rate for this "
                        "portfolio with `cap_rate_source`, so the code can value it on net "
                        "asset value beside the cash flows")
        return
    em.cap_rate = _fraction(raw["cap_rate"], "model.reit.cap_rate")
    if not 0.03 <= em.cap_rate <= 0.10:
        warnings.append(f"a cap rate of {em.cap_rate:.1%} is outside 3-10%; check the unit")
    if not str(raw.get("cap_rate_source") or "").strip():
        warnings.append("`model.reit.cap_rate` needs a `cap_rate_source`")
    em.other_assets = _number(raw.get("other_assets", 0.0), "model.reit.other_assets")
    preferred = _latest_balance(data, "Preferred Stock Equity") or 0.0
    em.nav = (em.years[0].lines["noi"] / em.cap_rate + em.other_assets - em.net_debt
              - preferred)


def implied_cap_rate(v: Valuation) -> float | None:
    """The cap rate today's price puts on the first forecast year's NOI."""
    em = v.equity
    if em.kind != "reit" or em.net_debt is None:
        return None
    market_cap = v.price * v.shares / v.fx / 1e6
    ev = market_cap + em.net_debt
    return em.years[0].lines["noi"] / ev if ev > 0 else None


def compute(model: dict, data: MarketData, peers: list | None, cost, shares: float,
            share_note: str) -> Valuation:
    warnings: list[str] = []
    notes: list[str] = [share_note] if share_note else []
    years = model.get("years")
    if not isinstance(years, list) or not years:
        raise ModelInputError("`model.years` must list the forecast years")
    n = len(years)
    lo, hi = val.YEARS_RANGE
    if not lo <= n <= hi:
        warnings.append(f"the forecast runs {n} years; use {lo} to {hi}")
    base_label = str(model.get("base_year") or "base year")
    kind, drivers = _drivers(model, n)
    for key, (a, b) in DRIVER_RANGES.items():
        for year, x in zip(years, drivers.get(key, [])):
            if not a - val.RANGE_TOLERANCE <= x <= b + val.RANGE_TOLERANCE:
                warnings.append(f"{year}: {key} of {x:.2%} is outside {a:.1%}-{b:.1%}; check "
                                "the unit (a fraction: 0.025 for 2.5%)")
                break
    if kind in ("bank", "insurer"):
        tax = [min(max(t, 0.0), 0.4) for t in _series(model, "tax_rate", n, fraction=True)]
        payout = _series(model, "payout", n, fraction=True)
        for year, p in zip(years, payout):
            if not PAYOUT_RANGE[0] <= p <= PAYOUT_RANGE[1]:
                raise ModelInputError(f"{year}: payout {p:.0%} is outside 0-150% of net income")
    else:
        tax, payout = [0.0] * n, [0.0] * n
    preferred = model.get("preferred_dividends")
    if preferred is None:
        preferred = _latest_income(data, "Preferred Stock Dividends") or 0.0
    preferred = abs(_number(preferred, "model.preferred_dividends"))

    book0, tangible0 = base_book(data)
    assets_key = {"bank": "earning_assets", "insurer": "invested_assets",
                  "utility": "rate_base"}.get(kind)
    assets0 = model.get(f"base_{assets_key}") if assets_key else None
    assets0 = _number(assets0, f"model.base_{assets_key}") if assets0 is not None else None
    if kind == "utility" and not assets0:
        raise ModelInputError("`model.base_rate_base` is missing: the base year's rate base "
                              "from the filing or rate case, so book can grow with it")
    cet1, capital_note, capital_warnings = _capital(model, data, kind, assets0)
    warnings += capital_warnings

    ke = cost_of_equity(model, cost)
    if cost is not None:
        warnings += cost.warnings
    terminal = model.get("terminal") or {}
    return_key = "investment_return" if kind == "reit" else "roe"
    if not isinstance(terminal, dict):
        raise ModelInputError(f"`model.terminal` must be an object with `{return_key}` and "
                              "`growth`")
    new_debt_rate = ((data.risk_free if data.risk_free is not None else config.RISK_FREE_DEFAULT)
                     + config.DEBT_SPREAD)
    raw_kind = model.get(kind) or {}
    if kind == "reit" and raw_kind.get("new_debt_rate") is not None:
        # The company's own marginal cost: a recent bond's coupon, sourced.
        new_debt_rate = _fraction(raw_kind["new_debt_rate"], "model.reit.new_debt_rate")
        if not str(raw_kind.get("new_debt_rate_source") or "").strip():
            warnings.append("`model.reit.new_debt_rate` needs a `new_debt_rate_source`: the "
                            "coupon of a recent bond issue")
    levered = None
    if kind == "reit":
        # What new investment earns for shareholders after the forecast follows from inputs
        # the model already has: the final cap rate, levered at its debt share and the cost
        # of new debt. O's writer of 2026-10-12 gave the unlevered 7.2% instead of ~8.5%,
        # which made all growth destroy value.
        cap, share = drivers["acquisition_cap_rate"][-1], drivers["debt_funded"][-1]
        levered = (cap - share * new_debt_rate) / (1 - share) if share < 1 else None
        if terminal.get(return_key) is None and levered is not None:
            terminal = {**terminal, return_key: levered}
        elif levered is not None and terminal.get(return_key) is not None:
            given = _fraction(terminal[return_key], f"model.terminal.{return_key}")
            if abs(given - levered) > 0.01 and len(" ".join(
                    str(terminal.get("rationale") or "").split()).split()) < 8:
                warnings.append(
                    f"terminal `investment_return` of {given:.1%} is not the {levered:.1%} "
                    f"that a {cap:.1%} cap rate earns shareholders at {share:.0%} debt costing "
                    f"{new_debt_rate:.1%}; leave it out and the code uses that, or argue the "
                    "difference in `terminal.rationale`")
        first_interest = drivers["interest_expense"][0]
        if first_interest > 0 and drivers["interest_expense"][-1] > 1.25 * first_interest:
            warnings.append(
                f"`interest_expense` rises from {first_interest:,.0f} to "
                f"{drivers['interest_expense'][-1]:,.0f}: it is the existing debt's interest "
                "(refinancing included); the code adds interest on the debt that funds new "
                "investment, so including it here counts it twice")
    if terminal.get("growth") is None or terminal.get(return_key) is None:
        raise ModelInputError(
            f"`model.terminal` needs `{return_key}` (" + (
                "the return new investment earns for shareholders after the forecast"
                if kind == "reit" else "the sustainable return on equity after the forecast")
            + ") and `growth`")
    growth = _fraction(terminal["growth"], "model.terminal.growth")
    terminal_roe = _fraction(terminal[return_key], f"model.terminal.{return_key}")

    inp = {"kind": kind, "years": [str(y) for y in years], "drivers": drivers, "tax": tax,
           "payout": payout, "preferred": preferred, "book0": book0, "tangible0": tangible0,
           "assets0": assets0, "cet1": cet1,
           # New debt costs the risk-free rate plus the house spread, before tax: a REIT
           # pays none.
           "new_debt_rate": new_debt_rate}
    em = build(inp, ke, growth, terminal_roe)
    em.reported = reported(data)
    em.capital_note = capital_note

    # Checks an analyst's reviewer would make.
    rf = data.risk_free if data.risk_free is not None else config.RISK_FREE_DEFAULT
    if growth >= rf:
        warnings.append(f"terminal growth of {growth:.1%} is not below the {rf:.2%} risk-free "
                        "rate; lower it beneath that yield")
    if growth > val.MAX_TERMINAL_GROWTH:
        warnings.append(f"terminal growth of {growth:.1%} is above {val.MAX_TERMINAL_GROWTH:.0%}")
    why_terminal = " ".join(str(terminal.get("rationale") or terminal.get("roe_rationale")
                                or "").split())
    final_roe = em.years[-1].roe
    if kind == "reit":
        if terminal_roe - ke > TERMINAL_SPREAD_MAX / 2 and len(why_terminal.split()) < 8:
            warnings.append(f"new investment earning {terminal_roe:.1%} for shareholders for "
                            f"ever is {(terminal_roe - ke) * 100:.0f}pp above the {ke:.1%} cost "
                            "of equity; argue it in `terminal.rationale` or lower it")
    elif abs(terminal_roe - final_roe) > TERMINAL_ROE_GAP and len(why_terminal.split()) < 8:
        warnings.append(f"terminal ROE of {terminal_roe:.1%} is {(terminal_roe - final_roe) * 100:+.1f}pp "
                        f"from the {final_roe:.1%} the drivers give in {em.years[-1].label}; "
                        "bring them together or say in `terminal.rationale` what moves it")
    if kind != "reit" and terminal_roe - ke > TERMINAL_SPREAD_MAX:
        warnings.append(f"terminal ROE of {terminal_roe:.1%} is {(terminal_roe - ke) * 100:.0f}pp "
                        f"above the {ke:.1%} cost of equity for ever; competition takes most "
                        "of that away, so lower it or argue the moat in `terminal.rationale`")
    record = [y["roe"] for y in em.reported if y.get("roe") is not None]
    why_roe = " ".join(str(model.get("roe_rationale") or "").split())
    if record and kind != "reit":
        best = max(record)
        peak = max(em.years, key=lambda y: y.roe)
        if peak.roe > best + ROE_ABOVE_RECORD:
            text = (f"ROE reaches {peak.roe:.1%} in {peak.label}, above the best reported "
                    f"{best:.1%}")
            if len(why_roe.split()) >= 8:
                notes.append(text + f": {why_roe}")
            else:
                warnings.append(text + "; say in `roe_rationale` which driver earns it")
        # As far below the record is as much a claim: NEE's of 2026-10-12 forecast 9-10% ROE
        # against 13-17% reported, because it left out its unregulated business.
        worst = min(record)
        top = max(y.roe for y in em.years)
        if top < worst - ROE_ABOVE_RECORD and len(why_roe.split()) < 8:
            warnings.append(f"ROE never rises above {top:.1%}, below the worst reported "
                            f"{worst:.1%}: check the forecast leaves no business out, or say in "
                            "`roe_rationale` why returns fall")
    bridge = " ".join(str(model.get("first_year_rationale") or "").split())
    bridged = len(bridge.split()) >= 8
    first = em.years[0]
    reported_ni = em.reported[-1].get("net_income") if em.reported else None
    if reported_ni and reported_ni > 0 and kind != "reit":
        jump = first.net_income / reported_ni - 1
        # Past this, a sentence does not excuse it: ALL's of 2026-10-12 put 16.4bn on 13.3bn
        # of trailing earnings with a bridge that itself annualised to 11.3bn.
        latest = max(reported_ni, data.ttm_net_income or 0.0)
        if first.net_income > (1 + FIRST_YEAR_HARD) * latest:
            warnings.append(f"{first.label} net income of {first.net_income:,.0f} is "
                            f"{first.net_income / latest - 1:+.0%} above the {latest:,.0f} the "
                            "company earned in its latest year or last four quarters: the "
                            "drivers overstate where it starts")
        elif abs(jump) > FIRST_YEAR_NI_JUMP:
            (notes.append(f"{first.label} net income moves {jump:+.0%} from {base_label}: {bridge}")
             if bridged else warnings.append(
                f"{first.label} net income of {first.net_income:,.0f} is {jump:+.0%} from the "
                f"{reported_ni:,.0f} reported for {base_label} ({data.reporting_currency} mn); "
                "check the drivers or bridge it in `model.first_year_rationale`"))
    reported_nii = em.reported[-1].get("nii") if em.reported else None
    if kind == "bank" and reported_nii and reported_nii > 0:
        jump = first.lines["nii"] / reported_nii - 1
        if abs(jump) > FIRST_YEAR_JUMP and not bridged:
            warnings.append(f"{first.label} net interest income of {first.lines['nii']:,.0f} "
                            f"is {jump:+.0%} from the {reported_nii:,.0f} reported; earning "
                            "assets x NIM should start from where the bank is, or "
                            "`first_year_rationale` should say why not")
    if cet1:
        low = [y for y in em.years if y.cet1_ratio is not None
               and y.cet1_ratio < cet1["requirement"]]
        if low:
            warnings.append(f"CET1 falls to {low[0].cet1_ratio:.1%} in {low[0].label}, below "
                            f"the {cet1['requirement']:.1%} requirement: the payout cannot be "
                            "that high, or the bank must raise capital")
        final = em.years[-1]
        surplus = (final.cet1_ratio or 0) - cet1["requirement"]
        if final.cet1_ratio is not None and surplus > EXCESS_CET1:
            excess = surplus * cet1["rwa"] * (final.lines["earning_assets"] / assets0
                                              if kind == "bank" and assets0 else 1.0)
            notes.append(f"CET1 reaches {final.cet1_ratio:.1%} in {final.label}, "
                         f"{surplus * 100:.1f}pp above the requirement: about "
                         f"{data.reporting_currency} {excess:,.0f}mn of surplus capital the "
                         "payout could return; the text should say why it is kept")
    if kind == "utility" and reported_ni and reported_ni > 0:
        # The base year rebuilt from the drivers must explain what the company earned: the
        # rate base alone left NEE's unregulated renewables business out.
        rebuilt = (assets0 * drivers["equity_ratio"][0] * drivers["earned_roe"][0]
                   + drivers["other_income"][0])
        if rebuilt < (1 - FIRST_YEAR_JUMP) * reported_ni and not bridged:
            warnings.append(
                f"the base rate base at the first year's equity ratio and earned ROE, plus "
                f"other income, gives {data.reporting_currency} {rebuilt:,.0f}mn against "
                f"{reported_ni:,.0f}mn of net income reported for {base_label}: "
                f"{reported_ni - rebuilt:,.0f}mn is unexplained. Put unregulated and "
                "contracted businesses' earnings in `other_income` (or value them in "
                "`parts`), or bridge the gap in `first_year_rationale`")
    if kind == "utility":
        if "allowed_roe" in drivers:
            over = [(y, e, a) for y, e, a in zip(years, drivers["earned_roe"],
                                                 drivers["allowed_roe"])
                    if e > a + EARNED_OVER_ALLOWED]
            if over and len(why_roe.split()) < 8:
                y, e, a = over[0]
                warnings.append(f"{y}: earned ROE of {e:.1%} is above the {a:.1%} allowed; "
                                "utilities rarely out-earn their allowance for long, so say in "
                                "`roe_rationale` how this one does")
        else:
            warnings.append("`model.utility.allowed_roe` is missing: give the allowed ROE from "
                            "the latest rate orders, so the earned ROE can be read against it")
        issued = -sum(min(y.distributions, 0.0) for y in em.years)
        dividends = sum(max(y.distributions, 0.0) for y in em.years)
        if issued > 0:
            notes.append(f"the rate base plan needs {data.reporting_currency} "
                         f"{issued:,.0f}mn of new equity over the forecast beyond retained "
                         "earnings: years with a negative distribution are equity issued")
        em.capital_note = (f"Book grows with the equity share of the rate base; distributions "
                           f"total {data.reporting_currency} {dividends:,.0f}mn, net of "
                           f"{issued:,.0f}mn of new equity.")
    if kind == "reit":
        em.capital_note = ("Distributable cash is AFFO less the equity-funded share of net "
                           "investment; the debt-funded share adds to debt, whose cost is in "
                           "the interest expense you forecast.")
        nav_inputs(em, model, data, warnings)

    per_share = em.value * 1e6 * data.fx / shares
    upside = (per_share / data.price - 1) * 100
    if kind == "insurer" and first.pretax > 0 and             first.lines["other_income"] > OTHER_INCOME_SHARE * first.pretax:
        warnings.append(f"other income of {first.lines['other_income']:,.0f} is "
                        f"{first.lines['other_income'] / first.pretax:.0%} of {first.label} "
                        "pre-tax income: `other_income` is the other businesses' pre-tax "
                        "profit, not their revenue")

    def far(reference: float | None) -> bool:
        return bool(reference) and not (1 / val.DIVERGENCE_FACTOR <= per_share / reference
                                         <= val.DIVERGENCE_FACTOR)

    if far(data.price) and (not data.analyst_target or far(data.analyst_target)):
        bps = em.book0 * 1e6 * data.fx / shares
        warnings.append(
            f"the implied price of {per_share:,.2f} is {val.pct(upside)} from the current "
            "price" + (f" and {per_share / data.analyst_target - 1:+.0%} from the Street "
                       f"average target of {data.analyst_target:,.2f}"
                       if data.analyst_target else "")
            + f", {per_share / bps:.1f}x book against {data.price / bps:.1f}x today. Check "
            "the inputs that drive it: the first year's earnings against what the company "
            "earns now, the ROE path and terminal ROE against the cost of equity. If it "
            "stands, the text must show what the market and the Street are missing")
    rows = [YearRow(y.label, y.revenue, y.pretax / y.revenue if y.revenue else 0.0, y.pretax,
                    y.net_income, 0.0, 0.0, y.net_income - y.distributions, y.distributions,
                    y.factor, y.distributions * y.factor) for y in em.years]
    base_revenue = _latest_income(data, "Total Revenue") or rows[0].revenue
    v = Valuation(
        currency=data.currency, reporting_currency=data.reporting_currency, fx=data.fx,
        fx_date=getattr(data, "fx_date", "") if data.reporting_currency != data.currency else "",
        base_label=base_label, base_revenue=base_revenue, rows=rows, wacc=ke,
        method="perpetuity", growth=growth, exit_multiple=None, pv_fcf=em.pv_excess,
        terminal_value=em.terminal_excess, pv_terminal=em.pv_terminal,
        enterprise_value=em.value, cash=0.0, debt=0.0, equity_value=em.value, shares=shares,
        price=data.price, per_share=per_share, upside_pct=upside,
        rating=val.rating_for(upside), cross_check_per_share=None,
        drivers=model.get("drivers") if isinstance(model.get("drivers"), dict) else {},
        warnings=warnings, notes=notes, cost=cost, ttm_ebitda=None,
        shares_from_market_cap=bool(share_note), quoted_ev_ebitda=None,
    )
    v.family, v.equity = "equity", em
    v.reported = list(reversed(market_data.reported_years(getattr(data, "prompt_block", "") or "")))
    v.street_target = data.analyst_target or None
    v.street_count = getattr(data, "analyst_count", None)
    v.events = [e for e in model.get("material_events") or [] if isinstance(e, dict)]
    v.method_choices = val._method_choices(model, years, base_label, glide=False)
    v.scenario_story = {"base": " ".join(str(model.get("base_rationale") or "").split())}
    v.stops = val.data_gates(data)
    implied_roe(v, data)
    if kind == "reit":
        # Book is depreciated cost for a REIT: price to book says little. NAV does the work.
        v.implied_margin = v.street_margin = None
        if em.nav and em.nav > 0:
            nav_ps = em.nav * 1e6 * data.fx / shares
            if abs(per_share / nav_ps - 1) > NAV_GAP_NOTE:
                notes.append(f"the value ({per_share:,.2f}) is {per_share / nav_ps - 1:+.0%} "
                             f"from net asset value ({nav_ps:,.2f} at a {em.cap_rate:.1%} cap "
                             "rate); the text should say which the market will close toward")
    if record and kind != "reit":
        v.record_margin = max(record)
    if kind in ("utility", "reit"):
        # Utilities and REITs carry real debt: their leverage is read as any company's.
        v.cash, v.debt = data.cash, data.debt
        v.minority = getattr(data, "minority_interest", 0.0) or 0.0
        v.ttm_ebitda = data.ttm_ebitda
        v.leverage = val.leverage(v, data)
    if peers:
        v.peers = val.peer_check(v, data, peers)
        pv = v.peers.value_pe if v.peers else None
        if v.peers and kind in ("bank", "insurer"):
            # The EV/EBITDA leg means nothing for a bank; P/E is the peer value.
            v.peers.value_ev_ebitda = None
            v.peers.median_ev_ebitda = None
        # Depreciation makes a REIT's P/E meaningless; it is priced on AFFO and NAV.
        if pv and kind != "reit" and abs(per_share / pv - 1) > val.PEER_GAP_NOTE:
            v.notes.append(f"the value ({per_share:,.2f}) is {per_share / pv - 1:+.0%} from the "
                           f"peer P/E value ({pv:,.2f}); the text should explain why")
    return v


# --------------------------------------------------------------------------- what-ifs

def revalue(v: Valuation, *, ke: float | None = None, growth: float | None = None,
            terminal_roe: float | None = None) -> float | None:
    em = v.equity
    try:
        m = build(em.inputs, em.ke if ke is None else ke,
                  em.growth if growth is None else growth,
                  em.terminal_roe if terminal_roe is None else terminal_roe)
    except (ModelInputError, ZeroDivisionError):
        return None
    return m.value * 1e6 * v.fx / v.shares


def book_per_share(v: Valuation, tangible: bool = False) -> float | None:
    em = v.equity
    book = em.tangible0 if tangible else em.book0
    if not book or book <= 0:
        return None
    return book * 1e6 * v.fx / v.shares


def implied_roe(v: Valuation, data: MarketData) -> None:
    """The sustainable ROE the price assumes, from P/B = (ROE - g) / (ke - g) on today's
    book; and the cost of equity at which the model gives the price."""
    em, bps = v.equity, book_per_share(v)
    if bps:
        v.implied_margin = em.growth + data.price / bps * (em.ke - em.growth)
        if data.analyst_target:
            v.street_margin = em.growth + data.analyst_target / bps * (em.ke - em.growth)
    v.implied_wacc = val._solve(lambda k: revalue(v, ke=k), data.price,
                                em.growth + val.MIN_WACC_GROWTH_SPREAD, 0.30)
    v.implied_terminal = val._solve(lambda r: revalue(v, terminal_roe=r), data.price,
                                    em.growth + 0.001, 0.60)


def apply_scenario(v: Valuation, model: dict, data: MarketData, name: str, cost) -> None:
    """A bull or bear case: drivers replaced, the rest of the base case kept."""
    raw = model.get(name) if name == "bull" else (model.get("bear") or model.get("downside"))
    is_bull = name == "bull"
    prefix = "bull" if is_bull else "bear"
    if not isinstance(raw, dict):
        v.warnings.append(f"`model.{name}` must be an object of inputs changed from the base case")
        return
    unknown = [k for k in raw if k not in SCENARIO_KEYS and not k.endswith("rationale")]
    if unknown:
        v.warnings.append(f"`model.{name}` has key(s) the code does not read ("
                          + ", ".join(unknown) + "); use " + ", ".join(sorted(SCENARIO_KEYS)))
    if not any(k in SCENARIO_KEYS for k in raw):
        v.warnings.append(f"`model.{name}` changes no input, so it is not a distinct scenario")
        return
    alt = {k: copy.deepcopy(x) for k, x in model.items()
           if k not in ("bull", "bear", "downside", "scenario_probabilities")}
    group = v.equity.kind
    changes: list[str] = []
    for key in SCENARIO_REPLACE:
        if raw.get(key) is not None and key in alt[group]:
            if key in MONEY_KEYS:
                # O's bear case of 2026-10-12 wrote net_investment: -0.4 for "40% less".
                given = raw[key] if isinstance(raw[key], list) else [raw[key]]
                base = alt[group][key] if isinstance(alt[group][key], list) else [alt[group][key]]
                scale = max((abs(x) for x in base if isinstance(x, (int, float))), default=0)
                if any(isinstance(x, (int, float)) and abs(x) < 0.01 * scale for x in given):
                    v.warnings.append(f"`model.{name}.{key}` of {raw[key]} looks like a change, "
                                      f"not an amount in {v.reporting_currency} mn: give "
                                      f"`{key}_change` (e.g. -0.4 for 40% less) or the amounts")
                    continue
            alt[group][key] = raw[key]
    for key, targets in SCENARIO_SCALE.items():
        if raw.get(key) is None or targets[0] not in alt[group]:
            continue
        change = _fraction(raw[key], f"model.{name}.{key}")
        for target in targets:
            series = alt[group].get(target)
            if series is not None:
                alt[group][target] = ([x * (1 + change) for x in series]
                                      if isinstance(series, list) else series * (1 + change))
        changes.append(f"{targets[0].replace('_', ' ')} {abs(change):.0%} "
                       f"{'above' if change > 0 else 'below'} the base case")
    if raw.get("payout") is not None:
        alt["payout"] = raw["payout"]
    if raw.get("terminal") is not None:
        if not isinstance(raw["terminal"], dict):
            v.warnings.append(f"`model.{name}.terminal` must be an object")
            return
        alt["terminal"] = {**(model.get("terminal") or {}), **raw["terminal"]}
    if raw.get("cost_of_equity") is not None:
        alt["cost_of_equity"] = raw["cost_of_equity"]
    try:
        d = compute(alt, data, None, cost if raw.get("cost_of_equity") is None else None,
                    v.shares, "")
    except (ModelInputError, TypeError) as exc:
        v.warnings.append(f"the {name} case could not be computed: {exc}")
        return
    de, last = d.equity, d.equity.years[-1]
    for key in SCENARIO_REPLACE:
        if raw.get(key) is not None and key in last.lines:
            x = last.lines[key]
            changes.append(f"{key.replace('_', ' ')} " + (
                val._money(x, v.reporting_currency) if key in MONEY_KEYS else f"{x:.2%}")
                + f" by {last.label}")
    if raw.get("payout") is not None:
        changes.append(f"payout {last.payout:.0%} by {last.label}")
    if raw.get("terminal") is not None:
        changes.append(f"terminal {TERMINAL_RETURN[de.kind]} {de.terminal_roe:.1%}, growth "
                       f"{de.growth:.1%}")
    if raw.get("cost_of_equity") is not None:
        changes.append(f"cost of equity {de.ke:.1%}")
    v.scenario_terminal[prefix] = ("growth", de.growth)
    v.scenario_wacc[prefix] = de.ke
    v.scenario_story[prefix] = " ".join(str(raw.get("rationale") or "").split())
    dv = {f"{prefix}_g": f"{de.growth:.1%}", f"{prefix}_wacc": f"{de.ke:.1%}",
          f"{prefix}_terminal_roe": f"{de.terminal_roe:.1%}"}
    for y in de.years:
        dv.update(year_values(y, v.reporting_currency, f"{prefix}_"))
    v.equity.cases[prefix] = de
    if is_bull:
        v.bull_per_share, v.bull_changes, v.bull_rows, v.bull_values = (
            d.per_share, changes, d.rows, dv)
        if d.per_share <= v.per_share:
            v.warnings.append(f"the bull case comes out at {d.per_share:,.2f}, not above the "
                              f"base case of {v.per_share:,.2f}; change inputs for the better")
    else:
        for key, value in list(dv.items()):
            dv["downside_" + key[len("bear_"):]] = value
        v.downside_per_share, v.downside_changes, v.downside_rows, v.downside_values = (
            d.per_share, changes, d.rows, dv)
        if d.per_share >= v.per_share:
            v.warnings.append(f"the bear case comes out at {d.per_share:,.2f}, not below the "
                              f"base case of {v.per_share:,.2f}; change inputs for the worse")
    v.warnings += [w for w in d.warnings if w.startswith("CET1")]
    if abs((d.per_share - v.per_share) / v.price * 100) < 10:
        v.warnings.append(f"the {name} case is close to the base case in upside "
                          "assumptions; check that it represents a distinct scenario")


# --------------------------------------------------------------------------- tables

def _m(x) -> str:
    if not isinstance(x, (int, float)):
        return val.NA
    return val._fmt_mn(x if abs(x) >= 0.5 else 0.0)


def _r(x, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if isinstance(x, (int, float)) else val.NA


def history_table(v: Valuation) -> str:
    em = v.equity
    if not em.reported:
        return "*Reported book value and returns: n.a., the market data has no balance sheet.*"
    head = ["Reported", "Unit", *[y["label"] for y in em.reported]]
    rows = [["Net interest income", "mn", *[_m(y.get("nii")) for y in em.reported]],
            ["Net income to common", "mn", *[_m(y.get("net_income")) for y in em.reported]],
            ["Common equity", "mn", *[_m(y.get("book")) for y in em.reported]],
            ["Tangible book", "mn", *[_m(y.get("tangible")) for y in em.reported]],
            ["ROE (average equity)", "%", *[_r(y.get("roe")) for y in em.reported]]]
    if em.kind != "bank":
        rows.pop(0)
    return val._md_table(head, rows)


def build_table(v: Valuation) -> str:
    em = v.equity
    ys = em.years
    head = ["Earnings build", "Unit", *[y.label for y in ys]]
    if em.kind == "bank":
        lines = [("Earning assets (year end)", "mn", lambda y: _m(y.lines["earning_assets"])),
                 ("Net interest margin", "%", lambda y: _r(y.lines["nim"], 2)),
                 ("Net interest income", "mn", lambda y: _m(y.lines["nii"])),
                 ("Non-interest income", "mn", lambda y: _m(y.lines["non_interest_income"])),
                 ("Net revenue", "mn", lambda y: _m(y.revenue)),
                 ("Efficiency ratio", "%", lambda y: _r(y.lines["efficiency_ratio"])),
                 ("Operating expenses", "mn", lambda y: _m(-y.lines["opex"])),
                 ("Loans", "mn", lambda y: _m(y.lines["loans"])),
                 ("Credit cost (of loans)", "%", lambda y: _r(y.lines["credit_cost"], 2)),
                 ("Provisions", "mn", lambda y: _m(-y.lines["provisions"]))]
    elif em.kind == "utility":
        lines = [("Rate base (year end)", "mn", lambda y: _m(y.lines["rate_base"])),
                 ("Equity ratio", "%", lambda y: _r(y.lines["equity_ratio"])),
                 ("Allowed ROE", "%", lambda y: _r(y.lines.get("allowed_roe"))),
                 ("Earned ROE (regulated)", "%", lambda y: _r(y.lines["earned_roe"])),
                 ("Regulated net income", "mn", lambda y: _m(y.lines["regulated_income"])),
                 ("Holding company and other, after tax", "mn",
                  lambda y: _m(y.lines["other_income"]))]
    elif em.kind == "reit":
        lines = [("NOI, existing portfolio", "mn", lambda y: _m(y.lines["existing_noi"])),
                 ("NOI from new investment", "mn", lambda y: _m(y.lines["acquired_noi"])),
                 ("Net operating income", "mn", lambda y: _m(y.lines["noi"])),
                 ("Other income", "mn", lambda y: _m(y.lines["other_income"])),
                 ("G&A", "mn", lambda y: _m(-y.lines["g_and_a"])),
                 ("Interest expense (incl. new debt)", "mn",
                  lambda y: _m(-y.lines["interest_expense"])),
                 ("Maintenance capex", "mn", lambda y: _m(-y.lines["maintenance_capex"])),
                 ("AFFO", "mn", lambda y: _m(y.lines["affo"])),
                 ("AFFO per share", v.currency,
                  lambda y: val._price(y.lines["affo"] * 1e6 * v.fx / v.shares)),
                 ("Net investment", "mn", lambda y: _m(y.lines["net_investment"])),
                 ("Cap rate on new investment", "%",
                  lambda y: _r(y.lines["acquisition_cap_rate"], 2)),
                 ("Share funded with debt", "%", lambda y: _r(y.lines["debt_funded"], 0)),
                 ("Equity-funded investment", "mn", lambda y: _m(-y.lines["equity_funded"])),
                 ("Distributable cash to shareholders", "mn", lambda y: _m(y.distributions)),
                 ("Equity invested (book, opening)", "mn", lambda y: _m(y.book_open)),
                 ("AFFO return on equity invested", "%", lambda y: _r(y.roe))]
        return val._md_table(head, [[a, b, *[f(y) for y in ys]] for a, b, f in lines]) + (
            f"\n\n*{v.reporting_currency} mn. AFFO is net operating income less G&A, interest "
            "and maintenance capex. " + em.capital_note + "*")
    else:
        lines = [("Net premiums earned", "mn", lambda y: _m(y.lines["net_premiums"])),
                 ("Combined ratio", "%", lambda y: _r(y.lines["combined_ratio"])),
                 ("Underwriting result", "mn", lambda y: _m(y.lines["underwriting"])),
                 ("Invested assets", "mn", lambda y: _m(y.lines["invested_assets"])),
                 ("Investment yield", "%", lambda y: _r(y.lines["investment_yield"], 2)),
                 ("Investment income", "mn", lambda y: _m(y.lines["investment_income"])),
                 ("Other income", "mn", lambda y: _m(y.lines["other_income"]))]
    if em.kind != "utility":
        lines += [("Pre-tax income", "mn", lambda y: _m(y.pretax)),
                  ("Tax", "mn", lambda y: _m(-y.tax))]
    utility = em.kind == "utility"
    lines += [("Preferred dividends", "mn", lambda y: _m(-y.preferred)),
              ("Net income to common", "mn", lambda y: _m(y.net_income)),
              ("Opening common equity", "mn", lambda y: _m(y.book_open)),
              ("ROE (opening equity)", "%", lambda y: _r(y.roe)),
              ("ROTE (opening tangible)", "%", lambda y: _r(y.rote)),
              ("Payout (dividends less new equity)" if utility
               else "Payout (dividends and buybacks)", "%", lambda y: _r(y.payout, 0)),
              ("Distributions (negative: equity issued)" if utility else "Distributions", "mn",
               lambda y: _m(y.distributions)),
              ("Closing common equity", "mn", lambda y: _m(y.book_close))]
    if any(y.cet1_ratio is not None for y in ys):
        lines.append(("CET1 ratio", "%", lambda y: _r(y.cet1_ratio)))
    lead = {"bank": "Net interest income is earned on average earning assets. ",
            "insurer": "Investment income is earned on average invested assets. ",
            "utility": "Regulated income is the earned ROE on the equity share of the average "
                       "rate base, after tax. "}[em.kind]
    return val._md_table(head, [[a, b, *[f(y) for y in ys]] for a, b, f in lines]) + (
        f"\n\n*{v.reporting_currency} mn. " + lead + em.capital_note + "*")


def excess_table(v: Valuation) -> str:
    em = v.equity
    head = ["Excess returns", "Unit", *[y.label for y in em.years], "Terminal"]
    rows = [["Opening common equity", "mn", *[_m(y.book_open) for y in em.years],
             _m(em.years[-1].book_close)],
            ["ROE", "%", *[_r(y.roe) for y in em.years], _r(em.terminal_roe)],
            ["Cost of equity", "%", *[_r(em.ke) for _ in em.years], _r(em.ke)],
            ["Excess return", "mn", *[_m(y.excess_return) for y in em.years],
             _m(em.terminal_excess)],
            ["Discount factor", "x", *[f"{y.factor:.3f}" for y in em.years],
             f"{em.years[-1].factor:.3f}"],
            ["Present value", "mn", *[_m(y.excess_return * y.factor) for y in em.years],
             _m(em.pv_terminal)]]
    if em.kind == "reit":
        rows[0][0], rows[1][0] = "Opening equity invested", "AFFO return on equity invested"
        rows[1][-1] = _r(em.terminal_roe) + " on new"
        return val._md_table(head, rows) + (
            f"\n\n*Excess return = (AFFO return - cost of equity) x equity invested; with "
            "book plus excess returns this equals the distributable cash discounted at the cost "
            f"of equity. After {em.years[-1].label}, AFFO grows {em.growth:.1%} a year and new "
            f"investment earns {em.terminal_roe:.1%}, so {em.terminal_payout:.0%} of AFFO is "
            "distributable.*")
    return val._md_table(head, rows) + (
        f"\n\n*Excess return = (ROE - cost of equity) x opening common equity. The terminal "
        f"value is {em.terminal_roe:.1%} ROE for ever, growing {em.growth:.1%}, at the end of "
        f"{em.years[-1].label}: (ROE - ke) x book / (ke - g). The payout that growth leaves is "
        f"{em.terminal_payout:.0%}.*")


def intrinsic_table(v: Valuation) -> str:
    em, cur, rep = v.equity, v.currency, v.reporting_currency
    bps, tbps = book_per_share(v), book_per_share(v, tangible=True)
    ni1 = em.years[0].net_income * 1e6 * v.fx / v.shares
    rows = [[f"Common equity today ({rep} mn)", "mn", _m(em.book0)],
            [f"(+) PV of excess returns {em.years[0].label}-{em.years[-1].label}", "mn",
             _m(em.pv_excess)],
            ["(+) PV of terminal excess returns", "mn", _m(em.pv_terminal)],
            ["(=) Equity value", "mn", _m(em.value)],
            ["Shares outstanding", "mn", f"{v.shares / 1e6:,.1f}"],
            ["Value per share (base case)", cur, val._price(v.per_share)],
            ["Current price", cur, val._price(v.price)],
            ["Upside (base case)", "%", val._signed(v.upside_pct)],
            ["Implied price to book", "x", f"{v.per_share / bps:.2f}x" if bps else val.NA],
            ["Implied price to tangible book", "x",
             f"{v.per_share / tbps:.2f}x" if tbps else val.NA],
            [f"Implied P/E on {em.years[0].label}", "x",
             f"{v.per_share / ni1:.1f}x" if ni1 > 0 else val.NA],
            ["Current price to book", "x", f"{v.price / bps:.2f}x" if bps else val.NA],
            ["Dividend discount on the same inputs", cur,
             val._price(em.ddm_value * 1e6 * v.fx / v.shares)]]
    if em.kind == "reit":
        # P/B and P/E mean little for a REIT; P/AFFO and NAV are what it is priced on.
        rows = [r for r in rows if "book" not in r[0] and "P/E" not in r[0]]
        affo1 = em.years[0].lines["affo"] * 1e6 * v.fx / v.shares
        if affo1 > 0:
            rows += [[f"Implied P/AFFO on {em.years[0].label}", "x", f"{v.per_share / affo1:.1f}x"],
                     [f"Current P/AFFO on {em.years[0].label}", "x", f"{v.price / affo1:.1f}x"]]
        if em.nav is not None:
            rows.append([f"NAV: {em.years[0].label} NOI at a {em.cap_rate:.1%} cap rate, less "
                         "net debt and preferred", cur, val._price(em.nav * 1e6 * v.fx / v.shares)])
        cap = implied_cap_rate(v)
        if cap is not None:
            rows.append(["Cap rate implied by today's price", "%", _r(cap, 2)])
    if v.weighted_per_share is not None:
        rows += [["Probability-weighted value", cur, val._price(v.weighted_per_share)],
                 ["Upside (weighted)", "%", val._signed(v.weighted_upside_pct)]]
    rows.append(["Rating", "", v.published_rating])
    return val._md_table(["Intrinsic Valuation", "Unit", "Value"], rows)


def scenarios_table(v: Valuation) -> str:
    em, cur, rep = v.equity, v.currency, v.reporting_currency
    cases = ("bear", "base", "bull")
    models = {"base": em, **em.cases}
    mid = min(2, len(em.years) - 1)
    ml, fl = em.years[mid].label, em.years[-1].label
    value = {"bear": v.downside_per_share, "base": v.per_share, "bull": v.bull_per_share}
    probs = v.scenario_probabilities

    def per(f):
        return [f(models[k]) if k in models else val.NA for k in cases]

    rows = [["What happens", "", *[v.scenario_story.get(k) or val.NA for k in cases]],
            ["Probability", "%", *[f"{probs[k]:.0%}" if k in probs else val.NA for k in cases]],
            [f"Net income, {fl}", f"{rep} bn", *per(lambda m: val._bn(m.years[-1].net_income))],
            [f"ROE, {ml}", "%", *per(lambda m: _r(m.years[mid].roe))],
            [f"ROE, {fl}", "%", *per(lambda m: _r(m.years[-1].roe))],
            ["Cost of equity", "%", *per(lambda m: _r(m.ke))],
            [f"Terminal {TERMINAL_RETURN[em.kind]}", "%", *per(lambda m: _r(m.terminal_roe))],
            ["Terminal growth", "%", *per(lambda m: _r(m.growth))],
            ["**Value per share**", cur, *[f"**{val._price(value[k])}**" if value[k] is not None
                                           else val.NA for k in cases]],
            ["Against the price", "%", *[val._signed((value[k] / v.price - 1) * 100)
                                         if value[k] is not None else val.NA for k in cases]]]
    weighted = (f"{val._price(v.weighted_per_share)} ({val._signed(v.weighted_upside_pct)})"
                if v.weighted_per_share is not None else val.NA)
    rows.append(["Probability-weighted value", cur, "", weighted, ""])
    return val._md_table(["Scenarios", "Unit", "Bear", "Base", "Bull"], rows)


def price_assumes_table(v: Valuation) -> str:
    em = v.equity
    if em.kind == "reit":
        rows = [["Return on new investment", "%", _r(v.implied_terminal), val.NA,
                 _r(em.terminal_roe)],
                ["Cost of equity", "%", _r(v.implied_wacc), val.NA, _r(em.ke)]]
        cap = implied_cap_rate(v)
        if cap is not None and em.cap_rate is not None:
            rows.append(["Cap rate", "%", _r(cap, 2), val.NA, _r(em.cap_rate, 2)])
        return val._md_table(["What the Price Assumes", "Unit", "Today's price",
                              "Street target", "My base case"], rows) + (
            "\n\n*Each solved alone, the other inputs at the base case; the cap rate is the "
            "first forecast year's NOI over today's market value plus net debt.*")
    rows = [["Sustainable ROE", "%", _r(v.implied_margin), _r(max(
        [y.get("roe") for y in em.reported if y.get("roe") is not None], default=None)),
        _r(v.street_margin) if v.street_target else val.NA, _r(em.terminal_roe)],
            ["Cost of equity", "%", _r(v.implied_wacc), "", val.NA, _r(em.ke)]]
    return val._md_table(["What the Price Assumes", "Unit", "Today's price", "Best reported",
                          "Street target", "My base case"], rows) + (
        "\n\n*Sustainable ROE solves price / book = (ROE - g) / (ke - g) on today's book at "
        "the base case's growth and cost of equity; the cost of equity row is the rate at "
        "which the model gives today's price.*")


def sensitivity_markdown(v: Valuation) -> str:
    em = v.equity
    roes = [em.terminal_roe + d for d in (-0.02, -0.01, 0.0, 0.01, 0.02)]
    kes = [em.ke + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01) if em.ke + d > 0.03]
    rows, cells, same = [], 0, 0
    base = val.rating_for(v.upside_pct)
    for ke in kes:
        line = [f"ke {ke:.1%}"]
        for roe in roes:
            x = revalue(v, ke=ke, terminal_roe=roe)
            text = val._price(x) if x is not None else "n.m."
            if x is not None:
                cells += 1
                same += val.rating_for((x / v.price - 1) * 100) == base
            if abs(ke - em.ke) < 1e-9 and abs(roe - em.terminal_roe) < 1e-9:
                text = f"**{text}**"
            line.append(text)
        rows.append(line)
    out = val._md_table([f"Value per Share ({v.currency}): cost of equity by terminal "
                         f"{TERMINAL_RETURN[em.kind]}",
                         *[f"{r:.1%}" for r in roes]], rows)
    if cells:
        out += (f"\n\n*The rating is unchanged in {same / cells:.0%} of these cost of equity "
                f"x terminal {TERMINAL_RETURN[em.kind]} cells (the conviction grid varies "
                "terminal growth instead); the base case is in bold.*")
    return out


def outputs_markdown(v: Valuation) -> str:
    md = ["### 1a. Book value and returns (reported)", history_table(v),
          "### 1b. Definitions", val.definitions_markdown(v), val.DEFINITIONS_SLOT,
          "### 2. Earnings build (base case)", "<!-- chart:revenue -->", build_table(v),
          "<!-- chart:roe -->",
          "### 3. Intrinsic Valuation (excess returns on equity)", intrinsic_table(v),
          "### 4. Excess Return Build", excess_table(v),
          "### 5. Scenarios and checks", scenarios_table(v), "<!-- chart:valuation -->",
          price_assumes_table(v), sensitivity_markdown(v), val.peers_markdown(v),
          val.checks_notes_markdown(v),
          "### 6. What would change my mind", val.THRESHOLDS_SLOT]
    return "\n\n".join(x for x in md if x)


# --------------------------------------------------------------------------- placeholders

def year_values(y: EquityYear, rep: str, prefix: str = "") -> dict[str, str]:
    """One forecast year's build as placeholders, by the model's own names: the JPM writer
    of 2026-10-12 reached for {{non_interest_income:FY2026}} and
    {{downside_credit_cost:FY2026}}."""
    out = {f"{prefix}net_income:{y.label}": val._money(y.net_income, rep),
           f"{prefix}roe:{y.label}": f"{y.roe:.1%}",
           f"{prefix}payout:{y.label}": f"{y.payout:.0%}",
           f"{prefix}book_value:{y.label}": val._money(y.book_close, rep),
           f"{prefix}distributions:{y.label}": val._money(y.distributions, rep)}
    if y.rote is not None:
        out[f"{prefix}rote:{y.label}"] = f"{y.rote:.1%}"
    if y.cet1_ratio is not None:
        out[f"{prefix}cet1_ratio:{y.label}"] = f"{y.cet1_ratio:.1%}"
    for key, x in y.lines.items():
        if key in ("nim", "credit_cost", "investment_yield"):
            out[f"{prefix}{key}:{y.label}"] = f"{x:.2%}"
        elif key in ("efficiency_ratio", "combined_ratio", "equity_ratio", "earned_roe",
                     "allowed_roe", "debt_funded", "acquisition_cap_rate"):
            out[f"{prefix}{key}:{y.label}"] = f"{x:.1%}"
        else:
            out[f"{prefix}{key}:{y.label}"] = val._money(x, rep)
    return out


def placeholders(v: Valuation) -> dict[str, str]:
    em, cur, rep = v.equity, v.currency, v.reporting_currency
    values = val._common_placeholders(v)
    bps, tbps = book_per_share(v), book_per_share(v, tangible=True)
    values.update({
        "cost_of_equity": f"{em.ke:.1%}", "ke": f"{em.ke:.1%}", "g": f"{em.growth:.1%}",
        "terminal_roe": f"{em.terminal_roe:.1%}", "terminal_payout": f"{em.terminal_payout:.0%}",
        "equity": val._money(em.value, rep), "book_value": val._money(em.book0, rep),
        "pv_excess": val._money(em.pv_excess, rep), "pv_tv": val._money(em.pv_terminal, rep),
        "ddm_value": f"{cur} {em.ddm_value * 1e6 * v.fx / v.shares:,.2f}",
    })
    if em.tangible0:
        values["tangible_book"] = val._money(em.tangible0, rep)
    if bps:
        values["book_per_share"] = f"{cur} {bps:,.2f}"
        values["pb"] = f"{v.price / bps:.2f}x"
        values["implied_pb"] = f"{v.per_share / bps:.2f}x"
    if tbps:
        values["tangible_book_per_share"] = f"{cur} {tbps:,.2f}"
        values["ptbv"] = f"{v.price / tbps:.2f}x"
        values["implied_ptbv"] = f"{v.per_share / tbps:.2f}x"
    if v.implied_margin is not None:
        values["implied_roe"] = f"{v.implied_margin:.1%}"
    if v.implied_wacc is not None:
        values["implied_ke"] = values["implied_wacc"] = f"{v.implied_wacc:.1%}"
    for y in em.years:
        values[f"net_income:{y.label}"] = val._money(y.net_income, rep)
        values[f"revenue:{y.label}"] = val._money(y.revenue, rep)
        values[f"pretax_income:{y.label}"] = val._money(y.pretax, rep)
        values[f"roe:{y.label}"] = f"{y.roe:.1%}"
        if y.rote is not None:
            values[f"rote:{y.label}"] = f"{y.rote:.1%}"
        values[f"payout:{y.label}"] = f"{y.payout:.0%}"
        values[f"book_value:{y.label}"] = val._money(y.book_close, rep)
        values[f"distributions:{y.label}"] = val._money(y.distributions, rep)
        values[f"excess_return:{y.label}"] = val._money(y.excess_return, rep)
        values.update(year_values(y, rep))
        if em.kind == "insurer":     # insurers call net investment income "NII"
            values[f"nii:{y.label}"] = val._money(y.lines["investment_income"], rep)
        # By its path in the model, as O's writer of 2026-10-12 cited {{reit.noi:FY2026}}.
        for key in y.lines:
            values[f"{em.kind}.{key}:{y.label}"] = values[f"{key}:{y.label}"]
    taxes = em.inputs.get("tax") or []
    if em.kind in ("bank", "insurer") and taxes and len(set(taxes)) == 1:
        values["tax_rate"] = f"{taxes[0]:.1%}"
    if v.leverage:
        lv = v.leverage
        values["net_debt"] = val._money(lv.net_debt, rep)
        values["cost_of_debt"] = f"{lv.cost_of_debt:.1%}"
        for name, x in (("net_debt_to_ebitda", lv.net_debt_to_ebitda),
                        ("interest_coverage", lv.interest_coverage)):
            if x is not None:
                values[name] = f"{x:.1f}x"
        if lv.interest_expense is not None:
            values["interest_expense"] = val._money(lv.interest_expense, rep)
    if v.current_ev_ebitda:
        values["ev_ebitda"] = f"{v.current_ev_ebitda:.1f}x"
    if v.peers and v.peers.median_ev_ebitda is not None:
        values["peer_ev_ebitda"] = f"{v.peers.median_ev_ebitda:.1f}x"
    base_assets = em.inputs.get("assets0")
    if base_assets and em.kind == "utility":
        values[f"rate_base:{v.base_label}"] = val._money(base_assets, rep)
    if em.kind == "reit":
        affo1 = em.years[0].lines["affo"] * 1e6 * v.fx / v.shares
        if affo1 > 0:
            values["p_affo"] = f"{v.price / affo1:.1f}x"
            values["implied_p_affo"] = f"{v.per_share / affo1:.1f}x"
            values["affo_yield"] = f"{affo1 / v.price:.1%}"
        if em.nav is not None:
            values["nav"] = val._money(em.nav, rep)
            values["nav_per_share"] = f"{cur} {em.nav * 1e6 * v.fx / v.shares:,.2f}"
            values["cap_rate"] = f"{em.cap_rate:.2%}"
        cap = implied_cap_rate(v)
        if cap is not None:
            values["implied_cap_rate"] = f"{cap:.2%}"
        values["investment_return"] = f"{em.terminal_roe:.1%}"
        for y in em.years:
            values[f"affo_per_share:{y.label}"] = (
                f"{cur} {y.lines['affo'] * 1e6 * v.fx / v.shares:,.2f}")
    for y in em.reported:
        if y.get("roe") is not None:
            values[f"roe:{y['label']}"] = f"{y['roe']:.1%}"
        if y.get("rote") is not None:
            values[f"rote:{y['label']}"] = f"{y['rote']:.1%}"
    if em.value:
        values["tv_share"] = f"{em.pv_terminal / em.value:.0%}"
    return values
