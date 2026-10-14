"""The DCF, computed in code from the assumptions the model chose.

The model is good at judging a business and bad at arithmetic. Left to do both, the
NVDA pitch of 2026-09-06 summed ten discounted cash flows to $1,990bn when they add to
$1,890bn, quoted three different FY2032 free cash flows, and put $225/HOLD on the cover
over a body that concluded $164 (-29%, a SELL under its own rule).

So the model now supplies only inputs, in a `model` object inside its JSON header, and
everything derived from them — free cash flow, present values, the bridge, the price
per share, the upside and the rating — is computed here. The balance sheet items and
share count in the bridge come from market data, never from the model.

All money values are in millions of the reporting currency, the unit of the MARKET
DATA statements. The per-share value is converted to the trading currency at the end.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import date
from dataclasses import dataclass, field

import config
import conglomerate
import market_data
import revenue_build
import valuation_route
from market_data import MarketData

RANGE_TOLERANCE = 0.001

# Recommendation rule, stated in the analyst prompt: upside above +25% is a BUY, below
# -10% a SELL, and anything in between a HOLD.
BUY_ABOVE = 25.0
SELL_BELOW = -10.0

# Sanity bounds. An input outside them does not stop the computation — the number is
# still produced — but it is reported, and the write turn is asked to fix or defend it.
WACC_RANGE = (0.06, 0.14)
MAX_TERMINAL_GROWTH = 0.04
MIN_WACC_GROWTH_SPREAD = 0.01   # g within 1pp of WACC sends the terminal value to infinity
EXIT_MULTIPLE_RANGE = (4.0, 40.0)
# A five- to seven-year explicit forecast, then the terminal value. Seven years is
# permitted when contracts, backlog or another named source supports the visibility.
YEARS_RANGE = (5, 10)
SEGMENT_TOLERANCE = 0.01        # segments must sum to total revenue within 1%
BASE_REVENUE_TOLERANCE = 0.05   # base year must match the latest reported revenue within 5%
# The first forecast year may not start this far below the last four reported quarters.
# The live NVDA run of 2026-09-23 forecast FY2027 at $233bn with $303bn already
# reported over the trailing four quarters, and the SELL followed from that alone.
TTM_FLOOR = 0.90
# A driver that names revenue growth may differ from the revenue line by this much (FX,
# a small acquisition) before the two contradict each other.
GROWTH_DRIVER_GAP_PP = 4.0
GROWTH_DRIVER = re.compile(r"\b(?:revenue|sales|top[- ]line)\b.*\bgrowth\b|\bgrowth\b.*\b(?:revenue|sales)\b",
                           re.I)
# D&A may run above capex while acquired intangibles amortise, but not in the final year:
# the terminal value repeats that year forever, so the gap becomes a cash flow that never
# needs reinvesting. The NFLX pitch of 2026-09-23 put content amortisation (37.5% of
# revenue) in D&A against 1.5% capex, for a 60% FCF margin on a 35% EBIT margin.
MAX_FINAL_DA_OVER_CAPEX = 0.05
# Capex at or above this share of revenue in the latest reported year puts D&A on a PP&E
# roll-forward instead of the writer's share of revenue. BABA's three runs of 2026-10-12
# put D&A at 3.8%, 12.0% and 12.4% of revenue against capex of 5-6% forever; for an
# asset-light business the writer's ratio is fine and the roll-forward adds nothing.
CAPEX_HEAVY_SHARE = 0.08
# Depreciation over opening net PP&E, as the company has reported it, held to this range.
PPE_DEPRECIATION_RANGE = (0.02, 0.50)
# A value this many times away from both the price and the Street average is more often
# a broken input than an insight. The NEE pitch of 2026-09-23 came out at USD 14 against
# a USD 78 price and a USD 98 Street target: $110bn of debt against a $137bn EV.
DIVERGENCE_FACTOR = 2.0
# The keys `model.terminal` may carry. Anything else is reported, not ignored: the NEE
# pitch of 2026-09-23 argued a 12x EV/EBITDA exit, the code only read `exit_ev_ebit`,
# and its cross-check silently vanished.
TERMINAL_KEYS = {"method", "growth", "exit_ev_ebit", "exit_ev_ebitda", "new_capital_return"}
# A terminal value whose implied EV/EBITDA is this far from the peer median is noted.
# NEE's paid 7.8x FY2030 EBITDA against a 12.3x peer median.
TERMINAL_PEER_GAP_NOTE = 0.35
# What `model.downside` may change. The bad case is the base model with these swapped,
# computed the same way, so the Risks section cites a price the code produced.
DOWNSIDE_KEYS = {"revenue_change", "ebit_margin", "capex_pct_revenue", "wacc", "terminal",
                 "price_deck"}


class ModelInputError(Exception):
    """The model's inputs cannot be turned into a valuation at all."""


@dataclass
class YearRow:
    label: str
    revenue: float
    ebit_margin: float
    ebit: float
    nopat: float
    da: float
    capex: float
    nwc_change: float
    fcf: float
    discount_factor: float
    pv_fcf: float

    @property
    def fcf_margin(self) -> float:
        return self.fcf / self.revenue if self.revenue else 0.0

    @property
    def ebitda(self) -> float:
        return self.ebit + self.da


@dataclass
class Valuation:
    currency: str               # trading currency: the per-share values
    reporting_currency: str     # the unit of every money amount below, in millions
    fx: float                   # one unit of reporting currency in trading currency
    base_label: str
    base_revenue: float
    rows: list[YearRow]
    wacc: float
    method: str                 # "perpetuity" or "exit_multiple"
    growth: float | None
    exit_multiple: float | None
    pv_fcf: float
    terminal_value: float
    pv_terminal: float
    enterprise_value: float
    cash: float
    debt: float
    equity_value: float
    shares: float               # absolute count
    price: float
    per_share: float
    upside_pct: float
    rating: str
    cross_check_per_share: float | None   # the other terminal method, when both are given
    segments: dict[str, list[float]] = field(default_factory=dict)
    # Each segment's revenue in the base year (model.segment_base), when the model gives it.
    segment_base: dict[str, float] = field(default_factory=dict)
    # D&A came from the PP&E roll-forward (`ppe_rollforward`), not `da_pct_revenue`.
    da_from_ppe: bool = False
    drivers: dict[str, dict] = field(default_factory=dict)
    # revenue_build.build(): the lines multiplied out, and any market chains.
    revenue_build: dict | None = None
    # conglomerate.apply_margins(): each segment's margin and EBIT, and the group's.
    segment_margins: dict | None = None
    # "sum_of_parts" when the rating follows the parts, not the DCF.
    primary: str = "dcf"
    warnings: list[str] = field(default_factory=list)   # worth a rewrite
    notes: list[str] = field(default_factory=list)      # worth knowing, not worth a rewrite
    peers: "PeerCheck | None" = None
    exit_basis: str = "EBIT"    # what the exit multiple multiplies: "EBIT" or "EBITDA"
    downside_per_share: float | None = None   # model.downside, computed like the base case
    downside_changes: list[str] = field(default_factory=list)
    downside_rows: list[YearRow] = field(default_factory=list)   # the bad case, year by year
    downside_values: dict[str, str] = field(default_factory=dict)  # its inputs, as placeholders
    bull_per_share: float | None = None
    bull_changes: list[str] = field(default_factory=list)
    bull_rows: list[YearRow] = field(default_factory=list)
    bull_values: dict[str, str] = field(default_factory=dict)
    scenario_probabilities: dict[str, float] = field(default_factory=dict)
    weighted_per_share: float | None = None
    weighted_upside_pct: float | None = None
    cost: "CostOfCapital | None" = None     # the house WACC, when the code set it
    ttm_ebitda: float | None = None           # the last four quarters, from market data
    quoted_ev_ebitda: float | None = None     # Yahoo's own EV/EBITDA, on its basis
    shares_from_market_cap: bool = False      # the provider's count was replaced
    fx_date: str = ""                         # the day the FX rate was read
    # A captive finance arm's debt, inside `debt` but left out of the bridge (reporting mn).
    finance_debt: float = 0.0
    # Outside owners' share of consolidated subsidiaries, taken off with the debt.
    minority: float = 0.0
    finance_debt_source: str = ""
    # Operating leases left out of `debt`: their cost is in operating income (US GAAP).
    operating_leases: float = 0.0
    # The per-year inputs (fractions), kept so the code can revalue the same forecast.
    inputs: dict = field(default_factory=dict)
    # The glide between the forecast and the perpetuity (config.GLIDE_YEARS), discounted
    # to the final forecast year; its value is inside `terminal_value`.
    glide_rows: list[YearRow] = field(default_factory=list)
    # What the price, and the Street's target, assume: the revenue CAGR to the final year
    # at which this model's margins and terminal give that value (None when none does).
    implied_cagr: float | None = None
    street_cagr: float | None = None
    # The rest of the reverse DCF, each solved alone with the other inputs at the base case:
    # the flat operating margin, the terminal growth or exit multiple, and the WACC at which
    # the model gives today's price. `record_cagr` is the reported revenue CAGR they are
    # read against, and `cagr_grade` how demanding the price's growth is next to it.
    implied_margin: float | None = None
    implied_terminal: float | None = None
    implied_wacc: float | None = None
    record_cagr: float | None = None
    cagr_grade: str | None = None
    # Failed data gates (data_gates). Any entry withholds the rating: the pitch ships
    # with no intrinsic price, upside or rating, and the gates on its cover.
    stops: list[str] = field(default_factory=list)
    # The value with the writer's WACC replaced by the house one, when the writer set its
    # own, so the reader sees what that choice changed (None when they match).
    house_per_share: float | None = None
    house_wacc: float | None = None
    # How far the call can be trusted, computed (never chosen): high, medium or low.
    conviction: str | None = None
    conviction_reasons: list[str] = field(default_factory=list)
    # What the business earns on the capital in it, and what borrowing does to it.
    returns: "Returns | None" = None
    new_capital_return: float | None = None   # what the terminal value's new capital earns
    leverage: "Leverage | None" = None
    # What the Model Outputs layout needs beyond the DCF itself (model_outputs_markdown).
    gross_margin: list[float] | None = None   # per forecast year; None when not given
    reported: list[dict] = field(default_factory=list)   # annual statements, oldest first
    street_target: float | None = None        # the Street average target, trading currency
    street_count: int | None = None
    # What the Street's target assumes, one input at a time, as reverse_dcf solves the price.
    street_margin: float | None = None
    street_terminal: float | None = None
    street_wacc: float | None = None
    record_margin: float | None = None        # the best reported operating margin
    margin_grade: str | None = None
    scenario_terminal: dict = field(default_factory=dict)   # case -> ("growth"|"multiple", x)
    scenario_wacc: dict = field(default_factory=dict)       # case -> WACC
    scenario_story: dict = field(default_factory=dict)      # case -> the writer's rationale
    method_choices: list = field(default_factory=list)      # (choice, the writer's reason)
    events: list = field(default_factory=list)              # model.material_events
    # "fcff", the enterprise DCF above, or "equity": a bank or insurer valued on excess
    # returns to equity (valuation_equity.py), whose build is in `equity`. For "equity",
    # `wacc` is the cost of equity and each row a year of the bank: revenue is net revenue,
    # EBIT pre-tax income, NOPAT net income to common, FCF the distributions.
    family: str = "fcff"
    equity: object = None
    # A loss-making or distressed company (distress_inputs): its flags, losses carried
    # forward and failure inputs; and the DCF before the failure weighting.
    distress: dict = field(default_factory=dict)
    going_concern_per_share: float | None = None
    # A cyclical's margin cycle (cycle_check): mean, low, high, base and its position.
    cycle: dict | None = None
    # Stakes in other companies the bridge adds to equity (stake_values): book-value
    # investments and listed stakes at market, reporting mn; and the sum of the parts.
    investments: float = 0.0
    stakes: dict = field(default_factory=dict)
    parts: dict | None = None

    @property
    def published_rating(self) -> str:
        return "NO RATING" if self.stops else self.rating

    @property
    def model_cagr(self) -> float | None:
        n, end = len(self.rows), self.rows[-1].revenue
        if self.base_revenue <= 0 or end <= 0:
            return None
        return (end / self.base_revenue) ** (1 / n) - 1

    @property
    def bridge_debt(self) -> float:
        """The debt the bridge takes off enterprise value."""
        return self.debt - self.finance_debt

    @property
    def downside_pct(self) -> float | None:
        if self.downside_per_share is None:
            return None
        return (self.downside_per_share / self.price - 1) * 100

    @property
    def bull_pct(self) -> float | None:
        if self.bull_per_share is None:
            return None
        return (self.bull_per_share / self.price - 1) * 100

    @property
    def recommendation_value(self) -> float:
        if self.primary == conglomerate.SOTP and self.parts:
            return self.parts["per_share"]
        return self.weighted_per_share if self.weighted_per_share is not None else self.per_share

    @property
    def recommendation_upside(self) -> float:
        if self.primary == conglomerate.SOTP and self.parts:
            return (self.parts["per_share"] / self.price - 1) * 100
        return (self.weighted_upside_pct if self.weighted_upside_pct is not None
                else self.upside_pct)

    @property
    def terminal_share(self) -> float:
        return self.pv_terminal / self.enterprise_value if self.enterprise_value else 0.0

    @property
    def terminal_ev_ebitda(self) -> float | None:
        """The EV/EBITDA the terminal value pays for the final forecast year."""
        if self.family == "equity":
            return None
        ebitda = self.rows[-1].ebitda
        return self.terminal_value / ebitda if ebitda > 0 else None

    @property
    def implied_growth(self) -> float | None:
        """The growth forever on final-year FCF at which a perpetuity equals the terminal value.

        For an exit multiple this is what the multiple assumes. NEE's pitch of 2026-09-26
        paid 15x FY2030 EBITDA, which is 4.6% growth forever, and its reviewer could see only
        that the perpetuity cross-check came out 59% lower.
        """
        if self.family == "equity":
            return self.growth
        tv, fcf = self.terminal_value, self.rows[-1].fcf
        if tv <= 0 or fcf <= 0:
            return None
        return (tv * self.wacc - fcf) / (tv + fcf)

    @property
    def current_ev_ebitda(self) -> float | None:
        """What the market pays today: current EV over the last four quarters' EBITDA."""
        if self.family == "equity" and getattr(self.equity, "kind", "") in ("bank", "insurer"):
            return None
        if not self.ttm_ebitda or self.ttm_ebitda <= 0:
            return None
        market_cap = self.price * self.shares / self.fx / 1e6
        return ((market_cap + self.debt - self.cash + self.minority - self.investments)
                / self.ttm_ebitda)

    def row(self, label: str) -> YearRow | None:
        wanted = _norm_year(label)
        return next((r for r in self.rows if _norm_year(r.label) == wanted), None)


def rating_for(upside_pct: float) -> str:
    if upside_pct > BUY_ABOVE:
        return "BUY"
    if upside_pct < SELL_BELOW:
        return "SELL"
    return "HOLD"


def _norm_year(label: str) -> str:
    """'FY2032', 'FY32', '2032F' and '2032' all name the same forecast year."""
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    if len(digits) == 2:
        digits = "20" + digits
    return digits or str(label).strip().upper()


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelInputError(f"`{name}` must be a number, got {value!r}")
    if math.isnan(value) or math.isinf(value):
        raise ModelInputError(f"`{name}` is not a finite number")
    return float(value)


def pct(value: float) -> str:
    """A signed whole percentage. ANF's downside of -0.1% printed as '-0%'."""
    text = f"{value:+.0f}%"
    return "0%" if text in ("+0%", "-0%") else text


def _fraction(value, name: str) -> float:
    """Rates are fractions (0.15). A model that writes 15 for 15% is corrected, not failed."""
    v = _number(value, name)
    return v / 100 if abs(v) > 1.5 else v


def _series(model: dict, key: str, n: int, *, fraction: bool, required: bool = True,
            default: float | None = None) -> list[float]:
    """A per-year input given as a list of n values, or one value for every year."""
    raw = model.get(key)
    if raw is None:
        if required:
            raise ModelInputError(f"`model.{key}` is missing")
        return [default] * n
    convert = _fraction if fraction else _number
    if isinstance(raw, list):
        if len(raw) != n:
            raise ModelInputError(f"`model.{key}` has {len(raw)} values for {n} forecast years")
        return [convert(v, f"model.{key}[{i}]") for i, v in enumerate(raw)]
    return [convert(raw, f"model.{key}")] * n


def _check_material_events(model: dict, years: list) -> None:
    """Keep declared, dated thesis events out of an unsupported terminal value.

    The analyst may not stop the explicit forecast in the event year and immediately
    capitalize a steady state. A material event needs two forecast years after it so the
    revenue and margin path can show the aftermath before the terminal value begins.
    """
    events = model.get("material_events")
    if events is None:
        return
    if not isinstance(events, list):
        raise ModelInputError("`model.material_events` must be a list")
    labels = [_norm_year(str(y)) for y in years]
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            raise ModelInputError(f"`model.material_events[{i}]` must be an object")
        name = str(event.get("name") or "").strip()
        year = _norm_year(str(event.get("year") or ""))
        if not name or not year:
            raise ModelInputError(
                f"`model.material_events[{i}]` needs a name and a forecast year")
        if year not in labels:
            raise ModelInputError(
                f"material event '{name}' is dated {year}, outside the explicit forecast; "
                "extend the forecast to include the event and its aftermath before the "
                "terminal value")
        affected = event.get("affected_segments")
        if not isinstance(affected, list) or not affected:
            raise ModelInputError(
                f"material event '{name}' needs `affected_segments` (use `consolidated` "
                "when it affects the whole company)")
        segment_names = set((model.get("segments") or {}).keys())
        unknown = [s for s in affected if s != "consolidated" and s not in segment_names]
        if unknown:
            raise ModelInputError(
                f"material event '{name}' names segment(s) absent from `model.segments`: "
                + ", ".join(map(str, unknown)))
        evidence = str(event.get("evidence") or "").strip()
        if len(evidence.split()) < 8:
            raise ModelInputError(
                f"material event '{name}' needs `evidence` naming its source and expected "
                "economic effect")
        effect = str(event.get("modeled_effect") or "").strip()
        if len(effect.split()) < 8:
            raise ModelInputError(
                f"material event '{name}' needs `modeled_effect` explaining which forecast "
                "drivers or margins change")
        event_index = labels.index(year)
        post_years = len(labels) - event_index - 1
        minimum = event.get("post_event_years", 2)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
            raise ModelInputError(
                f"`model.material_events[{i}].post_event_years` must be an integer of at least 2")
        if post_years < minimum:
            raise ModelInputError(
                f"material event '{name}' occurs in {year}, leaving only {post_years} "
                f"forecast year(s) afterward; include at least {minimum} post-event years "
                "before the terminal value")


# The beta the writer may set in place of the house one (model.beta), with a reason.
BETA_RANGE = (0.4, 2.5)


@dataclass
class CostOfCapital:
    """The house WACC: every input from market data or a house constant, none chosen."""
    wacc: float
    risk_free: float
    risk_free_source: str
    beta_raw: float
    beta: float                 # the one used: Blume-adjusted, at least config.BETA_FLOOR
    beta_source: str
    erp: float
    cost_of_equity: float
    cost_of_debt: float         # after tax
    debt_weight: float
    premium: float = 0.0
    premium_rationale: str = ""
    bound: str = ""             # why the result was held to WACC_RANGE, if it was
    warnings: list[str] = field(default_factory=list)
    beta_adjusted: float | None = None   # 0.67 x raw + 0.33, before the floor

    def describe(self) -> str:
        adjusted = self.beta_adjusted if self.beta_adjusted is not None else self.beta
        how = (f"{self.beta_raw:.2f} raw, {adjusted:.2f} adjusted, replaced"
               if self.beta_source.startswith("analyst")
               else f"{self.beta_raw:.2f} raw, adjusted to {adjusted:.2f}, held at the "
               f"{self.beta:.2f} floor" if self.beta > adjusted + 1e-9
               else f"{self.beta_raw:.2f} raw, adjusted")
        text = (f"{self.risk_free:.1%} risk-free ({self.risk_free_source}) + "
                f"{self.beta:.2f} beta ({self.beta_source}; {how}) "
                f"x {self.erp:.1%} ERP = {self.cost_of_equity:.1%} cost of equity; "
                f"{self.cost_of_debt:.1%} after-tax cost of debt at {self.debt_weight:.0%} "
                "of capital")
        if self.premium:
            text += f"; +{self.premium:.1%} premium ({self.premium_rationale})"
        return text + (f"; {self.bound}" if self.bound else "")


def finance_debt_input(model: dict | None, data: MarketData) -> tuple[float, str, list[str]]:
    """`model.captive_finance_debt`: the debt of a finance arm that lends to customers.

    Consolidated debt includes it, but it funds the arm's loans and leases to customers,
    not the business being valued, and the arm's interest is already an operating cost:
    CAT reports "interest expense excluding Financial Products" below operating profit.
    The CAT pitch of 2026-10-01 took all $45.1bn of consolidated debt off its enterprise
    value. Returns (amount in reporting mn, source, warnings).
    """
    model = model or {}
    raw = model.get("captive_finance_debt")
    if raw is None:
        return 0.0, "", []
    amount = _number(raw, "model.captive_finance_debt")
    if amount < 0:
        raise ModelInputError("`model.captive_finance_debt` is the finance arm's debt in "
                              f"{data.reporting_currency or 'reporting currency'} mn; it "
                              "cannot be negative")
    source = str(model.get("captive_finance_source") or "").strip()
    warnings = []
    if amount and not source:
        warnings.append("`captive_finance_debt` needs a `captive_finance_source` naming the "
                        "filing and line it comes from")
    if amount > data.debt:
        warnings.append(f"`captive_finance_debt` of {amount:,.0f} is more than the total debt "
                        f"of {data.debt:,.0f} in market data; it was held to that")
        amount = data.debt
    return amount, source, warnings


def cost_of_capital(data: MarketData, model: dict | None = None) -> CostOfCapital:
    """The discount rate from market data, plus any premium the model argues for."""
    model = model or {}
    warnings: list[str] = []
    if data.risk_free is not None:
        rf, rf_source = data.risk_free, getattr(data, "risk_free_source", "") or "10-year Treasury"
    else:
        rf, rf_source = config.RISK_FREE_DEFAULT, "house default; Treasury yield unavailable"
    if data.beta is not None:
        raw, beta_source = data.beta, data.beta_source or "market data"
    else:
        raw, beta_source = config.DEFAULT_BETA, "beta unavailable, market assumed"
    adjusted = 0.67 * raw + 0.33
    beta = max(adjusted, config.BETA_FLOOR)
    if model.get("beta") is not None:
        chosen = _number(model["beta"], "model.beta")
        why = str(model.get("beta_rationale") or "").strip()
        if not BETA_RANGE[0] <= chosen <= BETA_RANGE[1]:
            warnings.append(f"`beta` of {chosen:.2f} is outside {BETA_RANGE[0]}-{BETA_RANGE[1]}; "
                            "the house beta was used")
        elif not why:
            warnings.append("`beta` needs a `beta_rationale` (peer betas, cyclicality) saying "
                            "why the regressed beta is wrong; the house beta was used")
        else:
            beta, beta_source = chosen, f"analyst: {why}"
    ke = rf + beta * config.EQUITY_RISK_PREMIUM
    kd = (rf + config.DEBT_SPREAD) * (1 - config.WACC_TAX_RATE)
    equity = data.market_cap or (data.price * data.shares_outstanding
                                 if data.shares_outstanding else 0.0)
    # A finance arm's debt funds its customers' loans, not the business the WACC is for.
    finance_debt, _, _ = finance_debt_input(model, data)
    debt = (data.debt - finance_debt) * 1e6 * data.fx   # reporting mn -> trading units
    weight = debt / (debt + equity) if debt > 0 and equity else 0.0
    wacc = (1 - weight) * ke + weight * kd

    premium, rationale = 0.0, str(model.get("wacc_rationale") or "").strip()
    if model.get("wacc_premium") is not None:
        premium = _fraction(model["wacc_premium"], "model.wacc_premium")
        if premium < -RANGE_TOLERANCE or premium > config.WACC_PREMIUM_MAX + RANGE_TOLERANCE:
            warnings.append(f"`wacc_premium` of {premium:.1%} is outside 0-"
                            f"{config.WACC_PREMIUM_MAX:.0%}; it was held to that range")
        premium = min(max(premium, 0.0), config.WACC_PREMIUM_MAX)
        if premium and not rationale:
            warnings.append("`wacc_premium` needs a `wacc_rationale` naming the risk the "
                            "beta misses")
    wacc += premium

    bound = ""
    if wacc < WACC_RANGE[0] - RANGE_TOLERANCE or wacc > WACC_RANGE[1] + RANGE_TOLERANCE:
        held = min(max(wacc, WACC_RANGE[0]), WACC_RANGE[1])
        bound = f"{wacc:.1%} computed, held to {held:.0%}"
        wacc = held
    else:
        wacc = min(max(wacc, WACC_RANGE[0]), WACC_RANGE[1])
    return CostOfCapital(wacc=wacc, risk_free=rf, risk_free_source=rf_source, beta_raw=raw,
                         beta=beta, beta_adjusted=adjusted, beta_source=beta_source,
                         erp=config.EQUITY_RISK_PREMIUM,
                         cost_of_equity=ke, cost_of_debt=kd, debt_weight=weight,
                         premium=premium, premium_rationale=rationale, bound=bound,
                         warnings=warnings)


# Hard data gates, after the Stock Thesis skill's. Comfort Systems' pitch published SELL at
# -89% because one quarter's revenue (USD 1.83bn) was read as the year (USD 9.10bn); a
# figure that fails one of these is a misread period, and no rating is published on it.
GATE_DROP = 0.40          # a year's revenue under 40% of the year before
GATE_QUARTER_SLACK = 1.001
GATE_MARKET_CAP = 0.05    # quoted market cap against price x shares
GATE_RESTATEMENT = 0.20   # the same year's revenue reported twice, this far apart


def data_gates(data: MarketData) -> list[str]:
    """Reported figures that cannot be right together. Each message withholds the rating."""
    out = []
    annual = data.revenue
    if not annual or annual <= 0:
        return ["the latest annual revenue is missing or not positive"]
    quarter = market_data.trailing_quarters(data.prompt_block, "Total Revenue", 1)
    if quarter and quarter > annual * GATE_QUARTER_SLACK:
        out.append(f"the latest quarter's revenue ({quarter:,.0f}mn) is larger than the "
                   f"annual figure ({annual:,.0f}mn), so the annual figure is a partial period")
    ttm = data.ttm_revenue
    if ttm and ttm > 0 and (ttm < GATE_DROP * annual or annual < GATE_DROP * ttm):
        out.append(f"trailing four quarters' revenue ({ttm:,.0f}mn) and the latest annual "
                   f"revenue ({annual:,.0f}mn) are too far apart: one is a partial period")
    years = [(label, rev) for label, rev, _ in data.history or []
             if isinstance(rev, (int, float)) and rev]
    for (a, ra), (b, rb) in zip(years, years[1:]):
        if ra > 0 and rb < GATE_DROP * ra:
            out.append(f"{b} revenue ({rb:,.0f}mn) is under {GATE_DROP:.0%} of {a} "
                       f"({ra:,.0f}mn): check for a partial period")
    # Use the same reconciled share count as the valuation. Some quote providers report
    # only one class of shares while market cap includes every class (for example NVO ADRs).
    # `share_count` already replaces a recoverable class-count mismatch with market cap / price
    # and exposes the replacement as a warning; gating on the raw provider count would
    # incorrectly withhold an otherwise computable rating.
    shares, _ = market_data.share_count(data.shares_outstanding, data.market_cap, data.price)
    cap = data.market_cap
    if shares and cap and data.price and abs(data.price * shares / cap - 1) > GATE_MARKET_CAP:
        out.append(f"price x shares ({data.price * shares / 1e6:,.0f}mn) is more than "
                   f"{GATE_MARKET_CAP:.0%} from the quoted market cap ({cap / 1e6:,.0f}mn): "
                   "the price, share count or currency is misread")
    seen: dict[str, float] = {}
    for label, rev, _ in data.history or []:
        if not isinstance(rev, (int, float)) or rev <= 0:
            continue
        key = _norm_year(str(label))
        if key in seen and abs(rev / seen[key] - 1) > GATE_RESTATEMENT:
            out.append(f"{label} revenue is reported as both {seen[key]:,.0f}mn and "
                       f"{rev:,.0f}mn: a restatement that large is treated as a misread")
        seen.setdefault(key, rev)
    ebitda, income = data.ttm_ebitda, data.ttm_net_income
    # Gains and tax credits can lift net income over EBITDA honestly; only a gap the
    # statement does not account for is a misread. Older replays lack the bridge figures.
    explained = market_data.net_income_gap(
        ebitda, income, getattr(data, "ttm_operating_income", None),
        getattr(data, "ttm_pretax_income", None), getattr(data, "ttm_tax", None))
    if (ebitda is not None and income is not None and income > 0 and ebitda <= income
            and not explained):
        out.append(f"trailing EBITDA ({ebitda:,.0f}mn) is not above trailing net income "
                   f"({income:,.0f}mn): one of the two is misread")
    return out


def stake_values(model: dict, data: MarketData, quotes: list, warnings: list[str]) -> dict:
    """What the shareholders own outside the business the DCF values.

    Long-term equity investments come in at book from the balance sheet. A listed stake
    the model names in `listed_stakes` ({name, ticker, shares (mn), carrying_value (reporting
    mn, as the filing carries it), source}) is valued at its share price instead, priced by
    the code; its carrying value comes out of the book figure so it is not counted twice."""
    book = max(getattr(data, "equity_investments", 0.0) or 0.0, 0.0)
    rep = data.reporting_currency
    listed, carried = [], 0.0
    raw = model.get("listed_stakes") or []
    if not isinstance(raw, list):
        warnings.append("`model.listed_stakes` must be a list of stakes")
        raw = []
    by_symbol = {q.symbol.upper(): q for q in quotes}
    for s in raw:
        if not isinstance(s, dict) or not s.get("ticker"):
            continue
        ticker = str(s["ticker"]).strip().upper()
        name = str(s.get("name") or ticker)
        quote = by_symbol.get(ticker)
        try:
            shares = _number(s.get("shares"), f"listed_stakes {ticker}.shares")
        except ModelInputError as exc:
            warnings.append(str(exc))
            continue
        if quote is None or not quote.price:
            warnings.append(f"listed stake {name} ({ticker}) could not be priced; it is left "
                            "out of the bridge")
            continue
        if not str(s.get("source") or "").strip():
            warnings.append(f"listed stake {name} needs a `source` for its share count")
        value = shares * quote.price
        if s.get("carrying_value") is not None:
            carried += max(_number(s["carrying_value"], f"listed_stakes {ticker}.carrying_value"),
                           0.0)
        elif book:
            warnings.append(f"listed stake {name} needs `carrying_value`, the amount the filing "
                            f"carries it at inside the {_money(book, rep)} of long-term "
                            "investments, so it is not counted at book and at market")
        listed.append({"name": name, "ticker": ticker, "shares": shares,
                       "price": quote.price, "value": value})
    book_left = max(book - carried, 0.0)
    return {"book": book_left, "listed": listed, "quotes": list(quotes),
            "total": book_left + sum(x["value"] for x in listed)}


# The sum of the parts this far from the consolidated DCF needs the text to explain it.
PARTS_GAP_NOTE = 0.20


def sum_of_parts(v: "Valuation", model: dict, data: MarketData) -> None:
    """`model.parts`: each segment on its own multiple, as a cross-check on the DCF.

    {"Cloud": {"metric": "EBITDA", "amount": 5200, "year": "FY2027", "multiple": 18,
    "source": "median of AMZN, MSFT, GOOGL cloud peers"}, ...}, with `corporate_costs`
    ({"amount": -800, "multiple": 10}) for the head office no segment carries. A part valued
    on a later year is discounted back at the WACC. Equity is then bridged as the DCF is."""
    raw = model.get("parts")
    industry = valuation_route.industry(data)
    if not isinstance(raw, dict) or not raw:
        if re.search(r"Conglomerates", industry, re.I):
            v.warnings.append("a conglomerate's segments earn different multiples: give "
                              "`model.parts` (per segment `metric`, `amount`, `year`, "
                              "`multiple`, `source`) and `corporate_costs`, so the code can "
                              "value the sum of the parts beside the DCF")
        return
    labels = [r.label for r in v.rows]
    rows, ev = [], 0.0
    for name, p in raw.items():
        if not isinstance(p, dict):
            continue
        try:
            amount = _number(p.get("amount"), f"model.parts.{name}.amount")
            multiple = _number(p.get("multiple"), f"model.parts.{name}.multiple")
        except ModelInputError as exc:
            v.warnings.append(str(exc))
            continue
        year = str(p.get("year") or labels[0])
        t = labels.index(year) if year in labels else 0
        value = amount * multiple / (1 + v.wacc) ** t
        if not str(p.get("source") or "").strip():
            v.warnings.append(f"part `{name}` needs a `source` for its multiple")
        rows.append({"name": str(name), "metric": str(p.get("metric") or "EBITDA"),
                     "amount": amount, "year": year, "multiple": multiple, "value": value})
        ev += value
    corp = model.get("corporate_costs")
    if isinstance(corp, dict) and corp.get("amount") is not None:
        first = corp["amount"][0] if isinstance(corp["amount"], list) and corp["amount"] \
            else corp["amount"]
        amount = -abs(_number(first, "model.corporate_costs.amount"))
        multiple = _number(corp.get("multiple", 0), "model.corporate_costs.multiple")
        rows.append({"name": "Corporate costs", "metric": "cost", "amount": amount,
                     "year": labels[0], "multiple": multiple, "value": amount * multiple})
        ev += amount * multiple
    if not rows:
        return
    rate = conglomerate.discount(model)
    gross = ev
    ev *= 1 - rate
    equity = ev + v.cash - v.bridge_debt - v.minority + v.investments
    per_share = _equity_per_share(equity, {**(v.inputs or {}), "failure": None}, v.fx,
                                  v.shares)
    v.parts = {"rows": rows, "ev": ev, "gross_ev": gross, "discount": rate,
               "equity": equity, "per_share": per_share}
    gap = per_share / v.per_share - 1 if v.per_share else 0.0
    if abs(gap) > PARTS_GAP_NOTE:
        v.notes.append(f"the sum of the parts ({per_share:,.2f}) is {gap:+.0%} from the DCF "
                       f"({v.per_share:,.2f}); the text should say which segment's multiple "
                       "or forecast explains the gap")


def parts_markdown(v: "Valuation") -> str:
    if not v.parts:
        return ""
    rep, cur = v.reporting_currency, v.currency
    rows = [[r["name"], f"{r['metric']}, {r['year']}", _bn(r["amount"]), f"{r['multiple']:.1f}x",
             _bn(r["value"])] for r in v.parts["rows"]]
    if v.parts.get("discount"):
        rows += [["Sum of the parts", "", "", "", _bn(v.parts["gross_ev"])],
                 [f"Conglomerate discount ({v.parts['discount']:.0%})", "", "", "",
                  _bn(v.parts["ev"] - v.parts["gross_ev"])]]
    rows += [["**Enterprise value**", "", "", "", f"**{_bn(v.parts['ev'])}**"],
             ["Equity value, same bridge as the DCF", "", "", "", _bn(v.parts["equity"])],
             [f"**Value per share ({cur})**", "", "", "",
              f"**{_price(v.parts['per_share'])}** "
              f"({_signed((v.parts['per_share'] / v.price - 1) * 100)})"]]
    return _md_table([f"Sum of the Parts ({rep} bn)", "Basis", "Amount", "Multiple", "Value"],
                     rows) + ("\n\n*" + ("The primary method: the rating follows the sum of "
                                     "the parts, and the DCF is the cross-check."
                                     if v.primary == conglomerate.SOTP else
                                     "A cross-check: the rating follows the DCF.")
                              + " Parts valued on a later year are discounted back at the "
                              "WACC.*")


def _factor(i: int, wacc: float, path: list[float] | None) -> float:
    """The discount factor for forecast year i (0-based). With a WACC path, each year is
    discounted at its own rate, the whole path shifted so its final year is `wacc`."""
    if not path:
        return 1 / (1 + wacc) ** (i + 1)
    shift = wacc - path[-1]
    factor = 1.0
    for rate in path[:i + 1]:
        factor /= 1 + rate + shift
    return factor


def leverage_path(model: dict, cost: "CostOfCapital | None", n: int,
                  warnings: list[str]) -> list[float] | None:
    """`model.debt_weight`: the target debt share of capital by year, for a company changing
    its capital structure (a leveraged recap, a deleveraging plan). Beta is unlevered at
    today's weight and relevered at each year's, so the cost of equity and the WACC move
    with the leverage; the terminal value is at the final year's WACC."""
    if model.get("debt_weight") is None:
        return None
    if cost is None:
        warnings.append("`model.debt_weight` needs the house cost of capital; it was ignored")
        return None
    weights = _series(model, "debt_weight", n, fraction=True)
    if any(not 0 <= w < 0.9 for w in weights):
        raise ModelInputError("`model.debt_weight` must be a debt share of capital below 0.9")
    if not str(model.get("debt_weight_source") or "").strip():
        warnings.append("`model.debt_weight` needs a `debt_weight_source`: the company's stated "
                        "leverage target or financing plan")
    t = config.WACC_TAX_RATE
    w0 = cost.debt_weight
    unlevered = cost.beta / (1 + (1 - t) * w0 / (1 - w0))
    path = []
    for w in weights:
        beta = unlevered * (1 + (1 - t) * w / (1 - w))
        ke = cost.risk_free + beta * cost.erp + cost.premium
        rate = (1 - w) * ke + w * cost.cost_of_debt
        path.append(min(max(rate, WACC_RANGE[0]), WACC_RANGE[1]))
    return path


def _nopat(ebit: float, tax: float, nol: float | None) -> tuple[float, float | None]:
    """Operating profit after tax. With losses carried forward (`nol`, None when the model
    gives none), profit is untaxed until they are used up and new losses add to them;
    without, a loss gets no tax credit."""
    if nol is None:
        return (ebit * (1 - tax) if ebit > 0 else ebit), None
    if ebit <= 0:
        return ebit, nol - ebit
    used = min(nol, ebit)
    return ebit - (ebit - used) * tax, nol - used


def _reinvestment(revenue: float, previous: float, capex_pct: float, nwc_pct: float,
                  da: float, s2c: float | None) -> tuple[float, float]:
    """Capex and the change in working capital for one year."""
    if s2c is not None:
        return da + max(revenue - previous, 0.0) / s2c, 0.0
    return revenue * capex_pct, (revenue - previous) * nwc_pct


def _equity_per_share(equity: float, inputs: dict, fx: float, shares: float) -> float:
    """Equity (reporting mn) per share, with new shares sold to fund the forecast's burn
    and the failure weighting, when the model has them."""
    raised, new = inputs.get("raised") or 0.0, inputs.get("new_shares") or 0.0
    value = (equity + raised) * 1e6 * fx / (shares + new)
    failure = inputs.get("failure")
    if failure:
        p, if_failed = failure
        value = (1 - p) * value + p * if_failed
    return value


# A margin range this wide, with a fall of at least CYCLE_FALL in one year, is a cycle even
# outside the cyclical industries (memory chips sit in "Semiconductors").
CYCLE_SWING = 0.12
CYCLE_FALL = 0.05
CYCLE_MIN_YEARS = 8
# The base year sits in the top quarter of its range: a peak until argued otherwise.
CYCLE_PEAK = 0.75
# The terminal margin may sit this far above the through-cycle mean before it needs a case.
CYCLE_ABOVE_MEAN = 0.02


def cycle_check(model: dict, data: MarketData, margin: list[float], base_label: str,
                warnings: list[str], notes: list[str]) -> dict | None:
    """Where the base year sits in the company's margin cycle, and whether the terminal
    year, which the value repeats for ever, assumes a peak."""
    block = getattr(data, "prompt_block", "") or ""
    margins = market_data.cycle_margins(block)
    if len(margins) < 3:
        return None
    values = [m for _, m in margins]
    falls = [b - a for a, b in zip(values, values[1:])]      # newest first: older - newer
    industry = valuation_route.industry(data)
    # Outside the cyclical industries a swing alone is not a cycle: a scaling loss-maker
    # (RKLB) or one acquisition's amortisation (AVGO's VMware year) swings too. It needs a
    # long history, positive margins and two separate falls.
    cyclical = bool(valuation_route.CYCLICAL_INDUSTRY.search(industry)) or (
        len(values) >= CYCLE_MIN_YEARS and min(values) > 0
        and max(values) - min(values) >= CYCLE_SWING
        and sum(f >= CYCLE_FALL for f in falls) >= 2)
    if not cyclical:
        return None
    lo, hi, mean = min(values), max(values), sum(values) / len(values)
    base = values[0]
    position = (base - lo) / (hi - lo) if hi > lo else 0.5
    span = f"{margins[-1][0]}-{margins[0][0]}"
    cycle = {"mean": mean, "low": lo, "high": hi, "base": base, "position": position,
             "years": len(values), "span": span}
    why = " ".join(str(model.get("cycle_rationale") or "").split())
    argued = len(why.split()) >= 8
    declared = str(model.get("cycle_position") or "").strip()
    where = (f"{base_label}'s operating margin of {base:.1%} sits at {position:.0%} of its "
             f"{span} range ({lo:.1%} to {hi:.1%}, mean {mean:.1%})")
    if position >= CYCLE_PEAK and not declared:
        warnings.append(where + ": near the top of the cycle. Declare `cycle_position` (peak, "
                        "mid or trough) and argue it in `cycle_rationale`; from a peak, fade "
                        "the margin toward the through-cycle mean")
    terminal = margin[-1]
    if terminal > hi + RANGE_TOLERANCE:
        text = (f"the terminal margin of {terminal:.1%} is above the best of the {span} cycle "
                f"({hi:.1%})")
        (notes.append(text + f": {why}") if argued else warnings.append(
            text + "; a cyclical's value repeats its final year for ever, so bring it inside "
            "the range or argue the structural change in `cycle_rationale`"))
    elif terminal > mean + CYCLE_ABOVE_MEAN:
        text = (f"the terminal margin of {terminal:.1%} is {(terminal - mean) * 100:.1f}pp above "
                f"the {span} mean of {mean:.1%}")
        (notes.append(text + f": {why}") if argued else warnings.append(
            text + "; say in `cycle_rationale` why the cycle's average no longer applies"))
    notes.append(where + (f", declared {declared}" if declared else ""))
    if valuation_route.COMMODITY_INDUSTRY.search(industry):
        deck = model.get("price_deck")
        if not isinstance(deck, dict) or not deck.get("values") or not deck.get("source"):
            warnings.append("a commodity producer's revenue is a price deck: give `price_deck` "
                            "with `commodity`, `unit`, `values` (one per forecast year) and "
                            "`source` (the futures strip or a named forecast)")
        else:
            cycle["deck"] = deck
            for case in ("bull", "bear"):
                raw = model.get(case) if case == "bull" else (model.get("bear")
                                                              or model.get("downside"))
                if isinstance(raw, dict) and not isinstance(raw.get("price_deck"), dict):
                    warnings.append(f"`model.{case}` keeps the base price deck: give it its own "
                                    "`price_deck` and the `revenue_change` it implies")
    return cycle


# A company is flagged loss-making on trailing losses, and distressed on net debt above
# this many times trailing EBITDA or operating profit below this many times interest.
DISTRESS_NET_DEBT_EBITDA = 4.0
DISTRESS_INTEREST_COVER = 1.5


def distress_flags(data: MarketData) -> list[str]:
    flags = []
    block = getattr(data, "prompt_block", "") or ""
    annual = market_data.reported_years(block)
    ebit = annual[0].get("ebit") if annual else None
    if (data.ttm_net_income is not None and data.ttm_net_income < 0) or (
            ebit is not None and ebit < 0):
        flags.append("loss_making")
    net_debt = (data.debt or 0.0) - (data.cash or 0.0)
    ebitda = data.ttm_ebitda
    interest = market_data.trailing_quarters(block, "Interest Expense")
    if net_debt > 0 and ((ebitda is not None and ebitda <= 0)
                         or (ebitda and net_debt / ebitda > DISTRESS_NET_DEBT_EBITDA)):
        flags.append("distressed")
    elif interest and ebit is not None and abs(interest) > 0 and \
            ebit / abs(interest) < DISTRESS_INTEREST_COVER and net_debt > 0:
        flags.append("distressed")
    return flags


def distress_inputs(model: dict, data: MarketData, warnings: list[str]) -> dict:
    """`model.nol` and `model.failure` for a loss-making or distressed company.

    A going-concern DCF of a company that may not survive overstates it: the published value
    is (1 - p) x the DCF + p x the equity if it fails, p sourced from a default table for
    its rating, its bond spread or its cash runway."""
    out: dict = {"flags": distress_flags(data)}
    if model.get("nol") is not None:
        out["nol"] = max(_number(model["nol"], "model.nol"), 0.0)
        out["nol_source"] = str(model.get("nol_source") or "").strip()
        if out["nol"] and not out["nol_source"]:
            warnings.append("`model.nol` needs a `nol_source` naming the filing's tax note")
    elif "loss_making" in out["flags"]:
        warnings.append("the company is loss-making: give `model.nol`, its tax losses carried "
                        "forward from the latest filing's tax note (0 if none), with "
                        "`nol_source`, so profit is untaxed until they are used up")
    raw = model.get("failure")
    if isinstance(raw, dict):
        p = _fraction(raw.get("probability"), "model.failure.probability")
        if not 0 <= p < 1:
            raise ModelInputError("`model.failure.probability` must be between 0 and 1")
        if_failed = max(_number(raw.get("equity_if_failed", 0.0),
                                "model.failure.equity_if_failed"), 0.0)
        source = " ".join(str(raw.get("source") or "").split())
        if not source:
            warnings.append("`model.failure` needs a `source` for its probability: the "
                            "cumulative default rate for the company's rating, its bond "
                            "spread, or its cash runway")
        out["failure"] = (p, if_failed)
        out["failure_source"] = source or "unsourced"
    elif out["flags"]:
        warnings.append(f"the company is {' and '.join(f.replace('_', '-') for f in out['flags'])}: "
                        "give `model.failure` with `probability` (sourced in `source`: a "
                        "default table for its rating, its bond spread or its cash runway), "
                        "`equity_if_failed` (reporting mn, often 0) and `rationale`")
    return out


def compute(model: dict, data: MarketData, peers: list | None = None,
            cost: CostOfCapital | None = None) -> Valuation:
    """Turn the model's inputs into a valuation. Raises ModelInputError when it cannot.

    With `cost`, the house cost of capital replaces any `wacc` the model gave.
    """
    if not isinstance(model, dict):
        raise ModelInputError("the JSON header has no `model` object")
    route = valuation_route.route(data)
    if not route.supported:
        raise ModelInputError(route.reasons[0])
    shares, share_note = _prepare(data)
    build = None
    if route.method == "equity":
        import valuation_equity
        result = valuation_equity.compute(model, data, peers, cost, shares, share_note)
    else:
        # Revenue built from volume x price becomes the segments every later step reads,
        # the bull and bear cases included, so it is applied once, here.
        model, build = _built_revenue(model, data.reporting_currency)
        try:
            model, margins = conglomerate.apply_margins(model)
        except conglomerate.MarginError as exc:
            raise ModelInputError(str(exc)) from exc
        result = _fcff(model, data, peers, cost, shares, share_note)
        if margins is not None:
            result.segment_margins = margins
            result.warnings += margins["warnings"]
    if route.method == "fcff":
        result.notes += route.reasons
    if build is not None:
        result.revenue_build = build
        result.warnings += build["warnings"]
    _finish(result, model, data, cost)
    return result


def _built_revenue(model: dict, currency: str = "") -> tuple[dict, dict | None]:
    """The model with `revenue_build` multiplied out into segments, and the build."""
    if model.get("revenue_build") is None:
        return model, None
    years = model.get("years")
    if not isinstance(years, list) or not years:
        raise ModelInputError("`model.years` must list the forecast years")
    base = _number(model.get("base_revenue"), "model.base_revenue")
    try:
        built, build = revenue_build.apply(model, base, len(years), currency)
    except revenue_build.BuildError as exc:
        raise ModelInputError(str(exc)) from exc
    built.pop("revenue_build", None)
    return built, build


def _prepare(data: MarketData) -> tuple[float, str]:
    """What every method needs before it runs: a price and a share count."""
    if not data.price:
        raise ModelInputError("no current share price in market data")
    shares, share_note = market_data.share_count(data.shares_outstanding, data.market_cap,
                                                 data.price)
    if not shares:
        raise ModelInputError("no share count in market data")
    return shares, share_note


def _fcff(model: dict, data: MarketData, peers: list | None, cost: CostOfCapital | None,
          shares: float, share_note: str) -> Valuation:
    """The enterprise DCF: free cash flow to the firm at one WACC, then the equity bridge."""
    warnings: list[str] = []
    finance_debt, finance_source, finance_warnings = finance_debt_input(model, data)
    warnings += finance_warnings
    bridge_debt = data.debt - finance_debt
    minority = getattr(data, "minority_interest", 0.0) or 0.0
    peers, quotes = ([p for p in peers or [] if not getattr(p, "stake", False)],
                     [p for p in peers or [] if getattr(p, "stake", False)])
    stakes = stake_values(model, data, quotes, warnings)
    investments = stakes["total"]

    years = model.get("years")
    if not isinstance(years, list) or not years:
        raise ModelInputError("`model.years` must list the forecast years")
    n = len(years)
    _check_material_events(model, years)
    lo, hi = YEARS_RANGE
    if not lo <= n <= hi:
        span = f"{lo}" if lo == hi else f"{lo} to {hi}"
        warnings.append(f"the forecast runs {n} years; use {span}")

    base_revenue = _number(model.get("base_revenue"), "model.base_revenue")
    base_label = str(model.get("base_year") or "base year")

    segments: dict[str, list[float]] = {}
    raw_segments = model.get("segments") or {}
    if not isinstance(raw_segments, dict):
        raise ModelInputError("`model.segments` must map segment names to yearly revenue")
    for name, values in raw_segments.items():
        if not isinstance(values, list) or len(values) != n:
            raise ModelInputError(f"segment `{name}` needs {n} yearly values")
        segments[str(name)] = [_number(v, f"segment {name}") for v in values]

    if model.get("revenue") is not None:
        revenue = _series(model, "revenue", n, fraction=False)
    elif segments:
        revenue = [sum(vals[i] for vals in segments.values()) for i in range(n)]
    else:
        raise ModelInputError("`model.revenue` is missing")

    # The segments in the reported base year, so the table's base column and each segment's
    # growth run from a reported figure: SPOT's of 2026-10-07 had none, printed "-" for FY2025
    # and gave Premium an 11.1% "CAGR" that ran from FY2026.
    segment_base: dict[str, float] = {}
    raw_base = model.get("segment_base")
    if raw_base is not None:
        if not isinstance(raw_base, dict):
            raise ModelInputError("`model.segment_base` must map segment names to base-year revenue")
        segment_base = {str(k): _number(x, f"segment_base {k}") for k, x in raw_base.items()}
        stray = [k for k in segment_base if k not in segments]
        if stray:
            warnings.append("`model.segment_base` names segment(s) not in `model.segments`: "
                            + ", ".join(stray))
        total = sum(segment_base.values())
        if base_revenue and abs(total - base_revenue) / abs(base_revenue) > SEGMENT_TOLERANCE:
            warnings.append(f"{base_label}: `segment_base` sums to {total:,.0f} but "
                            f"base_revenue is {base_revenue:,.0f} ({data.reporting_currency} mn)")

    if segments:
        for i, year in enumerate(years):
            total = sum(vals[i] for vals in segments.values())
            if revenue[i] and abs(total - revenue[i]) / abs(revenue[i]) > SEGMENT_TOLERANCE:
                warnings.append(f"{year}: segments sum to {total:,.0f} but revenue is "
                                f"{revenue[i]:,.0f} ({data.reporting_currency} mn)")

    # A disposal or spin-off leaves the reported year bigger than the business being
    # valued. SPGI's of 2026-10-02 kept the reported FY2025 (Mobility included), started
    # FY2026 5% below it without Mobility, and called that 7% organic growth; its revision
    # set a pro-forma base and was refused for not matching the reported year. A declared
    # `pro_forma_base` is the business continuing, and the checks scale to it.
    pro_forma = model.get("pro_forma_base")
    scale = 1.0
    if isinstance(pro_forma, dict) and data.revenue:
        reason = str(pro_forma.get("reason") or "").strip()
        reported = pro_forma.get("reported_revenue", data.revenue)
        reported_years = [y["label"] for y in market_data.reported_years(
            getattr(data, "prompt_block", "") or "")]
        if not reason:
            warnings.append("`pro_forma_base` needs a `reason`: the disposal or spin-off that "
                            "makes the base smaller than the reported year")
        elif reported_years and _norm_year(base_label) > _norm_year(reported_years[0]):
            # SPGI's facts-mode rewrite of 2026-10-02 based on FY2026 "per guidance".
            warnings.append(f"`pro_forma_base` restates a reported year, but {base_label} is "
                            f"not reported yet (the latest is {reported_years[0]}): set "
                            f"`base_year` to {reported_years[0]} and `base_revenue` to its "
                            "revenue without the business that left")
        elif isinstance(reported, (int, float)) and reported:
            scale = base_revenue / reported
    elif data.revenue and base_revenue and (
            abs(base_revenue - data.revenue) / abs(data.revenue) > BASE_REVENUE_TOLERANCE):
        warnings.append(f"base revenue {base_revenue:,.0f} does not match the latest reported "
                        f"annual revenue of {data.revenue:,.0f} ({data.reporting_currency} mn); "
                        "if a disposal or spin-off explains it, declare `pro_forma_base`")

    if data.ttm_revenue and revenue[0] < TTM_FLOOR * data.ttm_revenue * scale:
        warnings.append(f"{years[0]} revenue of {revenue[0]:,.0f} starts below the "
                        f"{data.ttm_revenue * scale:,.0f} already reported over the last four quarters "
                        f"({data.reporting_currency} mn); the forecast must start from where "
                        "the business is, or the text must show why revenue is about to fall")

    margin = _series(model, "ebit_margin", n, fraction=True)
    tax = _series(model, "tax_rate", n, fraction=True)
    da_pct = _series(model, "da_pct_revenue", n, fraction=True)
    # Reinvestment as revenue added per unit of capital invested, for a company with no
    # stable capex ratio yet: capex is then D&A plus growth / sales_to_capital, working
    # capital included.
    s2c = None
    if model.get("sales_to_capital") is not None:
        s2c = _series(model, "sales_to_capital", n, fraction=False)
        if any(x <= 0 for x in s2c):
            raise ModelInputError("`model.sales_to_capital` must be above zero")
        capex_pct = [0.0] * n
    else:
        capex_pct = _series(model, "capex_pct_revenue", n, fraction=True)
    da_note = ""
    rolled = ppe_rollforward(data, base_label, revenue, capex_pct) if s2c is None else None
    if rolled:
        da_pct, da_note = rolled
    distress = distress_inputs(model, data, warnings)
    # Working capital invested per unit of revenue growth. Zero is a real answer for
    # businesses paid up front, so it may be left out.
    nwc_pct = _series(model, "nwc_pct_revenue_change", n, fraction=True, required=False,
                      default=0.0)
    # Gross margin splits operating income into cost of goods sold and operating costs
    # for the income statement; it changes no cash flow.
    gross = None
    if model.get("gross_margin") is None:
        warnings.append("`model.gross_margin` is missing: give the gross margin for each "
                        "forecast year (a fraction), so the income statement can show cost of "
                        "goods sold and operating costs")
    else:
        gross = _series(model, "gross_margin", n, fraction=True)
        below = [f"{y} ({g:.1%} against {m:.1%})" for y, g, m in zip(years, gross, margin)
                 if g < m]
        if below:
            warnings.append("gross margin below the EBIT margin in " + ", ".join(below)
                            + ": operating costs cannot be negative")

    # A normal annual history is a useful guardrail for the first forecast year. Do not
    # compare a continuing business with a consolidated year that still contains a sold
    # or spun-off unit; the report labels that transition explicitly instead.
    annual = next((y for y in market_data.reported_years(
        getattr(data, "prompt_block", "") or "")
                   if _norm_year(y["label"]) == _norm_year(base_label)), None)
    # A first-year step the writer explains in `first_year_rationale` is shown as a note,
    # not sent back: NVO's of 2026-10-11 explained its FCF step-up in the text, and the
    # warning, which nothing could answer, still shipped as unresolved.
    bridge = " ".join(str(model.get("first_year_rationale") or "").split())
    bridged = len(bridge.split()) >= 8
    first_year_notes: list[str] = []
    if not isinstance(pro_forma, dict):
        if annual and annual.get("revenue"):
            for metric, reported, forecast in (("EBIT", annual.get("ebit"), margin[0]),):
                if reported is None:
                    continue
                historical = reported / annual["revenue"]
                jump_pp = (forecast - historical) * 100
                if abs(jump_pp) > 3.0 and bridged:
                    first_year_notes.append(f"the first forecast-year {metric} margin moves "
                                            f"{jump_pp:+.1f}pp from {base_label}: {bridge}")
                elif abs(jump_pp) > 3.0:
                    warnings.append(f"first forecast-year {metric} margin changes "
                                    f"{jump_pp:+.1f}pp from {base_label} ({historical:.1%} "
                                    f"to {forecast:.1%}); explain the step-up or step-down "
                                    "with comparable reported results or company guidance in "
                                    "`model.first_year_rationale`")

    gap = da_pct[-1] - capex_pct[-1]
    if s2c is None and gap > MAX_FINAL_DA_OVER_CAPEX:
        warnings.append(
            f"{years[-1]}: D&A of {da_pct[-1]:.1%} of revenue runs {gap * 100:.1f}pp above capex "
            f"of {capex_pct[-1]:.1%}, and the terminal value repeats that year forever. If "
            "D&A includes amortisation of content or acquired intangibles, put the cash spent "
            "replacing it in capex; otherwise bring D&A and capex together by the final year")

    for i, year in enumerate(years):
        if not -1 < margin[i] < 1:
            raise ModelInputError(f"{year}: EBIT margin {margin[i]:.2f} is not a fraction")
        if tax[i] < -RANGE_TOLERANCE or tax[i] > 0.4 + RANGE_TOLERANCE:
            warnings.append(f"{year}: tax rate {tax[i]:.0%} is outside 0-40%; it was held to that range")
        tax[i] = min(max(tax[i], 0.0), 0.4)

    if cost is not None:
        wacc = cost.wacc
        warnings += cost.warnings
        if cost.bound:
            warnings.append(f"house WACC was {cost.bound}")
    else:
        wacc = _fraction(model.get("wacc"), "model.wacc")
        if wacc < WACC_RANGE[0] - RANGE_TOLERANCE or wacc > WACC_RANGE[1] + RANGE_TOLERANCE:
            warnings.append(f"WACC of {wacc:.1%} is outside "
                            f"{WACC_RANGE[0]:.0%}-{WACC_RANGE[1]:.0%}; it was held to that range")
        wacc = min(max(wacc, WACC_RANGE[0]), WACC_RANGE[1])

    terminal = model.get("terminal") or {}
    if not isinstance(terminal, dict):
        raise ModelInputError("`model.terminal` must be an object")
    growth = terminal.get("growth")
    growth = _fraction(growth, "model.terminal.growth") if growth is not None else None
    unknown = [k for k in terminal if k not in TERMINAL_KEYS and not k.endswith("rationale")]
    if unknown:
        warnings.append(f"`model.terminal` has key(s) the code does not read ("
                        + ", ".join(unknown) + "), so they were ignored; use "
                        + ", ".join(sorted(TERMINAL_KEYS)))
    if terminal.get("exit_ev_ebit") is not None and terminal.get("exit_ev_ebitda") is not None:
        raise ModelInputError("give one exit multiple, `terminal.exit_ev_ebit` or "
                              "`terminal.exit_ev_ebitda`, not both")
    exit_basis = "EBITDA" if terminal.get("exit_ev_ebitda") is not None else "EBIT"
    key = f"exit_ev_{exit_basis.lower()}"
    multiple = terminal.get(key)
    multiple = _number(multiple, f"model.terminal.{key}") if multiple is not None else None
    method = terminal.get("method") or ("perpetuity" if growth is not None else "exit_multiple")
    if method not in ("perpetuity", "exit_multiple"):
        raise ModelInputError("`model.terminal.method` must be perpetuity or exit_multiple")
    if method == "perpetuity" and growth is None:
        raise ModelInputError("a perpetuity terminal value needs `terminal.growth`")
    if method == "exit_multiple" and multiple is None:
        raise ModelInputError("an exit-multiple terminal value needs `terminal.exit_ev_ebitda` "
                              "or `terminal.exit_ev_ebit`")
    if growth is not None:
        if growth > MAX_TERMINAL_GROWTH:
            warnings.append(f"terminal growth of {growth:.1%} is above {MAX_TERMINAL_GROWTH:.0%}")
        # Nominal growth forever above the risk-free rate of the forecast's own currency
        # means the economy it sits in outgrows its own government bond for ever.
        rf = data.risk_free if data.risk_free is not None else config.RISK_FREE_DEFAULT
        if growth >= rf:
            warnings.append(f"terminal growth of {growth:.1%} is not below the {rf:.2%} risk-free "
                            f"rate ({getattr(data, 'risk_free_source', '') or '10-year Treasury'}); "
                            "lower it beneath that yield")
        if wacc - growth < MIN_WACC_GROWTH_SPREAD:
            raise ModelInputError(f"terminal growth {growth:.1%} must sit at least 1pp below "
                                  f"the {wacc:.1%} WACC")
    if (multiple is not None and
            (multiple < EXIT_MULTIPLE_RANGE[0] - RANGE_TOLERANCE or
             multiple > EXIT_MULTIPLE_RANGE[1] + RANGE_TOLERANCE)):
        warnings.append(f"exit multiple of {multiple:.1f}x EV/{exit_basis} is outside "
                        f"{EXIT_MULTIPLE_RANGE[0]:.0f}-{EXIT_MULTIPLE_RANGE[1]:.0f}x; "
                        "it was held to that range")

    wacc_path = leverage_path(model, cost, n, warnings)
    if wacc_path:
        wacc = wacc_path[-1]
        if growth is not None and wacc - growth < MIN_WACC_GROWTH_SPREAD:
            raise ModelInputError(f"terminal growth {growth:.1%} must sit at least 1pp below "
                                  f"the final year's {wacc:.1%} WACC")
    cycle_notes: list[str] = []
    cycle = cycle_check(model, data, margin, base_label, warnings, cycle_notes)
    rows: list[YearRow] = []
    previous = base_revenue
    nol = distress.get("nol")
    for i, year in enumerate(years):
        ebit = revenue[i] * margin[i]
        nopat, nol = _nopat(ebit, tax[i], nol)
        da = revenue[i] * da_pct[i]
        capex, nwc_change = _reinvestment(revenue[i], previous, capex_pct[i], nwc_pct[i], da,
                                          s2c[i] if s2c else None)
        fcf = nopat + da - capex - nwc_change
        factor = _factor(i, wacc, wacc_path)
        rows.append(YearRow(str(year), revenue[i], margin[i], ebit, nopat, da, capex,
                            nwc_change, fcf, factor, fcf * factor))
        previous = revenue[i]

    # The first explicit-year FCF margin has the same basis check as EBIT. It catches
    # sudden cash-conversion jumps that the historical annual statements already expose.
    if not isinstance(pro_forma, dict) and annual and annual.get("revenue"):
        reported_fcf = annual.get("fcf")
        if reported_fcf is not None:
            historical = reported_fcf / annual["revenue"]
            forecast = rows[0].fcf_margin
            jump_pp = (forecast - historical) * 100
            if abs(jump_pp) > 3.0 and bridged:
                first_year_notes.append(f"the first forecast-year FCF margin moves "
                                        f"{jump_pp:+.1f}pp from {base_label} ({historical:.1%} "
                                        f"to {forecast:.1%}): {bridge}")
            elif abs(jump_pp) > 3.0:
                warnings.append(f"first forecast-year FCF margin changes {jump_pp:+.1f}pp "
                                f"from {base_label} ({historical:.1%} to {forecast:.1%}); "
                                "explain the cash-conversion change with comparable reported "
                                "results or company guidance in `model.first_year_rationale`")

    last = rows[-1]
    inputs = {"ebit_margin": margin, "tax": tax, "da_pct": da_pct, "capex_pct": capex_pct,
              "nwc_pct": nwc_pct,
              "pro_forma_reason": (str(pro_forma.get("reason") or "").strip()
                                   if isinstance(pro_forma, dict) else ""),
              "reported_base": (annual if annual and _norm_year(annual["label"])
                                == _norm_year(base_label) else None),
              "nol": distress.get("nol"), "s2c": s2c, "wacc_path": wacc_path}
    # The forecast's cash burn beyond the cash on hand is raised as new shares at today's
    # price: the DCF already charges the burn, so the new holders' cash is added to equity
    # and their shares to the count.
    burn, running = 0.0, 0.0
    for r in rows:
        running += r.fcf
        burn = min(burn, running)
    raised = max(0.0, -burn - max(data.cash, 0.0))
    inputs["raised"] = raised
    inputs["new_shares"] = raised * 1e6 * data.fx / data.price if raised else 0.0
    if distress.get("failure"):
        p, if_failed = distress["failure"]
        inputs["failure"] = (p, if_failed * 1e6 * data.fx / shares)
    prev_revenue = rows[-2].revenue if len(rows) > 1 else base_revenue
    glide_rows: list[YearRow] = []

    def terminal_value(kind: str) -> float:
        nonlocal glide_rows
        if kind == "perpetuity":
            value, glide = continuing_value(last, prev_revenue, wacc, growth, tax[-1],
                                            nwc_pct[-1], config.GLIDE_YEARS)
            if method == "perpetuity":
                glide_rows = glide
            return value
        return (last.ebitda if exit_basis == "EBITDA" else last.ebit) * multiple

    def per_share(tv: float) -> tuple[float, float, float]:
        ev = sum(r.pv_fcf for r in rows) + tv * last.discount_factor
        equity = ev + data.cash - bridge_debt - minority + investments
        return ev, equity, _equity_per_share(equity, inputs, data.fx, shares)

    tv = terminal_value(method)
    ev, equity, value = per_share(tv)
    pv_fcf = sum(r.pv_fcf for r in rows)

    cross = None
    other = "exit_multiple" if method == "perpetuity" else "perpetuity"
    if (other == "perpetuity" and growth is not None) or (other == "exit_multiple" and multiple):
        cross = per_share(terminal_value(other))[2]

    notes: list[str] = [share_note] if share_note else []
    if da_note:
        notes.append(da_note)
    notes += first_year_notes
    if tv * last.discount_factor > 0.85 * ev > 0:
        notes.append(f"the terminal value is {tv * last.discount_factor / ev:.0%} of "
                        "enterprise value; the explicit forecast carries almost none of it")
    if not data.cash_debt_known:
        notes.append("cash and debt were unavailable from market data and are taken as zero")

    upside = (value / data.price - 1) * 100
    drivers = model.get("drivers") if isinstance(model.get("drivers"), dict) else {}

    result = Valuation(
        currency=data.currency, reporting_currency=data.reporting_currency, fx=data.fx,
        fx_date=getattr(data, "fx_date", "") if data.reporting_currency != data.currency else "",
        base_label=base_label, base_revenue=base_revenue, rows=rows, wacc=wacc,
        method=method, growth=growth, exit_multiple=multiple, pv_fcf=pv_fcf,
        terminal_value=tv, pv_terminal=tv * last.discount_factor, enterprise_value=ev,
        cash=data.cash, debt=data.debt, finance_debt=finance_debt,
        operating_leases=getattr(data, "operating_leases", 0.0) or 0.0,
        finance_debt_source=finance_source, minority=minority, equity_value=equity,
        shares=shares,
        price=data.price, per_share=value, upside_pct=upside, rating=rating_for(upside),
        cross_check_per_share=cross, segments=segments, drivers=drivers, warnings=warnings,
        segment_base=segment_base, da_from_ppe=bool(da_note),
        notes=notes, exit_basis=exit_basis, cost=cost, ttm_ebitda=data.ttm_ebitda,
        shares_from_market_cap=bool(share_note),
        quoted_ev_ebitda=getattr(data, "quoted_ev_ebitda", None),
        inputs=inputs, glide_rows=glide_rows,
    )
    result.gross_margin = gross
    result.investments, result.stakes = investments, stakes
    result.distress = distress
    result.cycle = cycle
    notes += cycle_notes
    if raised:
        notes.append(f"the forecast burns {_money(-burn, data.reporting_currency)} of cash "
                     f"against {_money(max(data.cash, 0.0), data.reporting_currency)} on hand: "
                     f"the {_money(raised, data.reporting_currency)} shortfall is raised as "
                     f"{inputs['new_shares'] / 1e6:,.1f}mn new shares at today's price")
    if distress.get("failure"):
        p, _ = distress["failure"]
        gc = _equity_per_share(equity, {**inputs, "failure": None}, data.fx, shares)
        result.going_concern_per_share = gc
        notes.append(f"the going-concern DCF of {gc:,.2f} is weighted by a {p:.0%} chance of "
                     f"failure ({distress['failure_source']}), at "
                     f"{inputs['failure'][1]:,.2f} a share if it fails")
    result.reported = list(reversed(market_data.reported_years(
        getattr(data, "prompt_block", "") or "")))
    result.street_target = data.analyst_target or None
    result.street_count = getattr(data, "analyst_count", None)
    result.events = [e for e in model.get("material_events") or [] if isinstance(e, dict)]
    result.method_choices = _method_choices(model, years, base_label)
    result.scenario_story = {"base": " ".join(str(model.get("base_rationale") or "").split())}
    warnings += _layout_warnings(model, segments)
    result.stops = data_gates(data)
    result.implied_cagr = implied_revenue_cagr(result, data.price)
    if data.analyst_target:
        result.street_cagr = implied_revenue_cagr(result, data.analyst_target)
    reverse_dcf(result, data)
    warnings += _growth_driver_warnings(drivers, base_revenue, revenue, years)
    warnings += _driver_build_warnings(drivers, base_revenue, revenue, years,
                                       data.reporting_currency)
    warnings += _cash_conversion_warnings(result, data)
    result.returns = capital_returns(result, data)
    result.leverage = leverage(result, data)
    warnings += _reinvestment_warnings(result, terminal, data)
    notes += _returns_notes(result)
    implied_g = result.implied_growth
    if method == "exit_multiple" and implied_g is not None and implied_g > MAX_TERMINAL_GROWTH:
        notes.append(f"the {multiple:.1f}x exit multiple implies {implied_g:.1%} growth forever "
                     f"on {last.label} free cash flow, above the {MAX_TERMINAL_GROWTH:.0%} a "
                     "perpetuity may use; the text should say what sustains it")
    sum_of_parts(result, model, data)
    if peers:
        result.peers = peer_check(result, data, peers)
        pv = result.peers.value if result.peers else None
        if pv and abs(result.per_share / pv - 1) > PEER_GAP_NOTE:
            notes.append(f"the DCF value ({result.per_share:,.2f}) is "
                         f"{result.per_share / pv - 1:+.0%} from the peer-multiple value "
                         f"({pv:,.2f}); the text should explain why")

    # What the terminal value pays for the last year, against what the market pays the
    # peers today. Only a note on its own: a growth company's peers trade on trailing
    # multiples its terminal year should not (AVGO's peers were at 51x on 2026-09-24).
    implied = result.terminal_ev_ebitda
    peer_ev = result.peers.median_ev_ebitda if result.peers else None
    multiple_gap = ""
    if implied and peer_ev:
        multiple_gap = (f"the terminal value pays {implied:.1f}x {last.label} EBITDA against "
                        f"a peer median of {peer_ev:.1f}x today")
        if abs(implied / peer_ev - 1) > TERMINAL_PEER_GAP_NOTE:
            notes.append(multiple_gap + "; the text should say why")

    def far(reference: float | None) -> bool:
        return bool(reference) and not (1 / DIVERGENCE_FACTOR
                                         <= value / reference <= DIVERGENCE_FACTOR)

    if far(data.price) and (not data.analyst_target or far(data.analyst_target)):
        street = (f" and {value / data.analyst_target - 1:+.0%} from the Street average "
                  f"target of {data.analyst_target:,.2f}" if data.analyst_target else "")
        diagnosis = f" ({multiple_gap})" if multiple_gap else ""
        warnings.append(
            f"the implied price of {value:,.2f} is {pct(upside)} from the current price"
            f"{street}. Check the inputs that drive it: FCF margins against the business's "
            "real reinvestment needs, WACC, the terminal assumption" + diagnosis + ", and "
            "the debt in the bridge against enterprise value. If it stands, the text must "
            "show what the market and the Street are missing")
    return result


def _finish(result: Valuation, model: dict, data: MarketData,
            cost: CostOfCapital | None) -> None:
    """Scenarios, their probabilities, the rating, the house value and conviction."""
    apply = _apply_scenario
    if result.family == "equity":
        import valuation_equity
        apply = valuation_equity.apply_scenario
    if model.get("bull") is not None:
        apply(result, model, data, "bull", cost)
    if model.get("bear") is not None or model.get("downside") is not None:
        apply(result, model, data, "bear", cost)
    if result.bull_per_share is not None and result.downside_per_share is not None:
        if result.bull_rows and result.downside_rows:
            for base_row, bull_row, bear_row in zip(result.rows, result.bull_rows,
                                                    result.downside_rows):
                if not bear_row.revenue <= base_row.revenue <= bull_row.revenue:
                    result.warnings.append(
                        f"scenario revenue is out of order in {base_row.label}: bear "
                        f"{bear_row.revenue:,.0f}, base {base_row.revenue:,.0f}, bull "
                        f"{bull_row.revenue:,.0f} {result.reporting_currency}mn; expected "
                        "bear <= base <= bull")
        raw_probs = model.get("scenario_probabilities") or {}
        defaults = {"bull": 0.25, "base": 0.50, "bear": 0.25}
        if not isinstance(raw_probs, dict):
            raw_probs = {}
            result.warnings.append("scenario_probabilities must be an object; defaulted to 25/50/25")
        try:
            probs = {k: float(raw_probs.get(k, default)) for k, default in defaults.items()}
            if any(x < 0 for x in probs.values()):
                raise ValueError("probabilities cannot be negative")
        except (TypeError, ValueError):
            probs = defaults.copy()
            result.warnings.append("scenario probabilities were invalid; defaulted to 25/50/25")
        total = sum(probs.values())
        if total <= 0:
            probs = defaults.copy()
            result.warnings.append("scenario probabilities summed to zero; defaulted to 25/50/25")
        elif abs(total - 1.0) > 0.005:
            result.warnings.append(f"scenario probabilities sum to {total:.1%}, not 100%; "
                                   "they were normalized for the weighted value")
            probs = {k: x / total for k, x in probs.items()}
        result.scenario_probabilities = probs
        result.weighted_per_share = (probs["bull"] * result.bull_per_share
                                     + probs["base"] * result.per_share
                                     + probs["bear"] * result.downside_per_share)
        result.weighted_upside_pct = (result.weighted_per_share / result.price - 1) * 100
        result.rating = rating_for(result.weighted_upside_pct)
        if not result.bull_per_share >= result.per_share >= result.downside_per_share:
            result.warnings.append("scenario values are out of order; expected bull >= base >= bear")
    if conglomerate.is_primary(model) and result.parts:
        # The writer argued the parts value this company; the DCF stays as the check.
        result.primary = conglomerate.SOTP
        result.rating = rating_for(result.recommendation_upside)
        result.notes.append(f"the rating follows the sum of the parts "
                            f"({result.parts['per_share']:,.2f}, after a "
                            f"{result.parts['discount']:.0%} conglomerate discount); the DCF "
                            f"gives {result.per_share:,.2f}")
    house_value(result, data)
    conviction(result)


DRIVER_BASES = ("analyst", "sourced")
THRESHOLD_KEYS = ("measure", "latest", "wrong_if", "when", "claims")


def _layout_warnings(model: dict, segments: dict) -> list[str]:
    """The model fields the Model Outputs layout prints: each driver's definition and the
    segment it builds, the base-case story and the thresholds that would refute the call."""
    warnings = []
    drivers = model.get("drivers") if isinstance(model.get("drivers"), dict) else {}
    for name, spec in drivers.items():
        if not isinstance(spec, dict):
            continue
        if len(str(spec.get("definition") or "").split()) < 8:
            warnings.append(f"driver `{name}` needs a `definition`: what it counts, where the "
                            "figure comes from and how you project it")
        if str(spec.get("basis") or "").lower() not in DRIVER_BASES:
            warnings.append(f"driver `{name}` needs `basis`: \"sourced\" when its path is a "
                            "published forecast, \"analyst\" when it is your own")
        seg = spec.get("segment")
        if seg is not None and str(seg) not in segments:
            warnings.append(f"driver `{name}` names segment `{seg}`, which is not in "
                            "`model.segments`")
    if len(str(model.get("base_rationale") or "").split()) < 8:
        warnings.append("`model.base_rationale` is missing: one sentence on what happens in "
                        "the base case, as `bull` and `bear` have")
    thresholds = model.get("thresholds")
    if not isinstance(thresholds, list) or not thresholds:
        warnings.append("`model.thresholds` is missing: two to six measures that would prove "
                        "the thesis wrong, each with " + ", ".join(f"`{k}`" for k in THRESHOLD_KEYS))
    else:
        for i, t in enumerate(thresholds):
            missing = [k for k in THRESHOLD_KEYS
                       if not isinstance(t, dict) or not str(t.get(k) or "").strip()]
            if missing:
                warnings.append(f"`model.thresholds[{i}]` needs " + ", ".join(
                    f"`{k}`" for k in missing))
    return warnings


def _method_choices(model: dict, years: list, base_label: str,
                    glide: bool = True) -> list[tuple[str, str]]:
    """The writer's stated reasons for the choices the code does not make, by name."""
    choices = [("forecast length", f"{len(years)} years, {years[0]} to {years[-1]}, from "
                f"{base_label}" + (f", then a {config.GLIDE_YEARS}-year glide to the terminal "
                                   "rate" if config.GLIDE_YEARS and glide else ""))]
    for label, key in (("ROE", "roe_rationale"),):
        text = " ".join(str(model.get(key) or "").split())
        if text:
            choices.append((label, text))
    for label, key in (("first year", "first_year_rationale"), ("discount rate", "wacc_rationale"),
                       ("beta", "beta_rationale"),
                       ("probabilities", "scenario_probabilities_rationale")):
        text = " ".join(str(model.get(key) or "").split())
        if text:
            choices.append((label, text))
    terminal = model.get("terminal") if isinstance(model.get("terminal"), dict) else {}
    for key in ("rationale", "new_capital_rationale"):
        text = " ".join(str(terminal.get(key) or model.get(key) or "").split())
        if text:
            choices.append(("terminal" if key == "rationale" else "return on new capital", text))
    pro_forma = model.get("pro_forma_base")
    if isinstance(pro_forma, dict) and pro_forma.get("reason"):
        choices.append(("pro-forma base", str(pro_forma["reason"]).strip()))
    if model.get("captive_finance_source"):
        choices.append(("finance arm's debt", str(model["captive_finance_source"]).strip()))
    return choices


def house_value(v: Valuation, data: MarketData) -> None:
    """The base case at the house WACC (market beta, no premium), when the writer's differs."""
    try:
        house_cost = cost_of_capital(data, None)
    except ModelInputError:
        return
    house = house_cost.cost_of_equity if v.family == "equity" else house_cost.wacc
    if abs(house - v.wacc) < 0.0005:
        return
    v.house_wacc, v.house_per_share = house, _revalue(v, wacc=house)
    if v.house_per_share is None:
        v.house_wacc = None
        return
    up = (v.house_per_share / v.price - 1) * 100
    if rating_for(up) != rating_for(v.upside_pct):
        v.notes.append(f"at the house WACC of {house:.1%} the base case is "
                       f"{v.house_per_share:,.2f} ({pct(up)}, {rating_for(up)}), not "
                       f"{rating_for(v.upside_pct)}: the text should defend the departure")


# Conviction, computed: how much of the WACC x terminal grid keeps the rating, how far apart
# the scenarios are, and whether a data gate failed. Never chosen, never the upside's size.
CONVICTION_GRID_HIGH, CONVICTION_GRID_LOW = 0.8, 0.5
CONVICTION_SPREAD_HIGH = 0.6     # bull less bear, as a share of the price


def conviction(v: Valuation) -> None:
    cells, same = 0, 0
    for dw in (-0.01, -0.005, 0.0, 0.005, 0.01):
        for step in (-2, -1, 0, 1, 2):
            if v.method == "perpetuity":
                got = _revalue(v, wacc=v.wacc + dw, growth=(v.growth or 0) + step * 0.005)
            else:
                got = _revalue(v, wacc=v.wacc + dw, multiple=v.exit_multiple * (1 + step * 0.1))
            if got is None:
                continue
            cells += 1
            same += rating_for((got / v.price - 1) * 100) == rating_for(v.upside_pct)
    reasons = []
    share = same / cells if cells else 0.0
    if cells:
        grid = ("cost of equity x terminal growth" if v.family == "equity"
                else "WACC x terminal")
        reasons.append(f"the base rating holds in {share:.0%} of the {grid} grid")
    spread = None
    if v.bull_per_share is not None and v.downside_per_share is not None:
        spread = (v.bull_per_share - v.downside_per_share) / v.price
        reasons.append(f"bull to bear spans {spread:.0%} of the price")
    if v.stops:
        reasons.append(f"{len(v.stops)} data gate(s) failed")
    if v.stops or share < CONVICTION_GRID_LOW:
        level = "low"
    elif share >= CONVICTION_GRID_HIGH and (spread is None or spread <= CONVICTION_SPREAD_HIGH):
        level = "high"
    else:
        level = "medium"
    v.conviction, v.conviction_reasons = level, reasons


def continuing_value(last: YearRow, prev_revenue: float, wacc: float, growth: float,
                     tax: float, nwc_pct: float, years: int) -> tuple[float, list[YearRow]]:
    """Every cash flow after the final forecast year, valued at the end of that year.

    `years` of glide come first: revenue growth steps down evenly from the final year's to
    `growth`, with the final year's margin, tax, D&A, capex and working-capital ratios; then
    a perpetuity at `growth` on the last glide year. With no glide this is the plain
    perpetuity on the final year's free cash flow. The rows are discounted to the final
    forecast year, not to today.
    """
    if years <= 0 or last.revenue <= 0:
        return last.fcf * (1 + growth) / (wacc - growth), []
    start = last.revenue / prev_revenue - 1 if prev_revenue > 0 else growth
    da_pct, capex_pct = last.da / last.revenue, last.capex / last.revenue
    digits = _norm_year(last.label)
    rows, revenue = [], last.revenue
    for k in range(1, years + 1):
        g = start + (growth - start) * k / years
        new = revenue * (1 + g)
        ebit = new * last.ebit_margin
        nopat = ebit * (1 - tax) if ebit > 0 else ebit
        nwc_change = (new - revenue) * nwc_pct
        fcf = nopat + new * da_pct - new * capex_pct - nwc_change
        factor = 1 / (1 + wacc) ** k
        label = f"FY{int(digits) + k}" if digits.isdigit() else f"{last.label}+{k}"
        rows.append(YearRow(label, new, last.ebit_margin, ebit, nopat, new * da_pct,
                            new * capex_pct, nwc_change, fcf, factor, fcf * factor))
        revenue = new
    perpetuity = rows[-1].fcf * (1 + growth) / (wacc - growth)
    return sum(r.pv_fcf for r in rows) + perpetuity / (1 + wacc) ** years, rows


def _revalue(v: Valuation, revenue: list[float] | None = None, *,
             margin: float | None = None, wacc: float | None = None,
             growth: float | None = None, multiple: float | None = None) -> float | None:
    """The per-share value of this model with one input changed, every other kept: another
    revenue path, a flat operating margin, a WACC, or a terminal growth or exit multiple."""
    if v.family == "equity":
        if revenue is not None or margin is not None or multiple is not None:
            return None
        import valuation_equity
        return valuation_equity.revalue(v, ke=wacc, growth=growth)
    inp = v.inputs
    if not inp:
        return None
    revenue = revenue or [r.revenue for r in v.rows]
    r_ = v.wacc if wacc is None else wacc
    g = v.growth if growth is None else growth
    if v.method == "perpetuity" and r_ - g < MIN_WACC_GROWTH_SPREAD:
        return None
    rows, previous = [], v.base_revenue
    nol, s2c = inp.get("nol"), inp.get("s2c")
    for i, r in enumerate(v.rows):
        rev = revenue[i]
        m = inp["ebit_margin"][i] if margin is None else margin
        ebit = rev * m
        nopat, nol = _nopat(ebit, inp["tax"][i], nol)
        da = rev * inp["da_pct"][i]
        capex, nwc = _reinvestment(rev, previous, inp["capex_pct"][i], inp["nwc_pct"][i], da,
                                   s2c[i] if s2c else None)
        fcf = nopat + da - capex - nwc
        factor = _factor(i, r_, inp.get("wacc_path"))
        rows.append(YearRow(r.label, rev, m, ebit, nopat, da, capex, nwc, fcf, factor,
                            fcf * factor))
        previous = rev
    last = rows[-1]
    if v.method == "perpetuity":
        prev = rows[-2].revenue if len(rows) > 1 else v.base_revenue
        tv, _ = continuing_value(last, prev, r_, g, inp["tax"][-1],
                                 inp["nwc_pct"][-1], config.GLIDE_YEARS)
    else:
        tv = (last.ebitda if v.exit_basis == "EBITDA" else last.ebit) \
            * (v.exit_multiple if multiple is None else multiple)
    ev = sum(r.pv_fcf for r in rows) + tv * last.discount_factor
    return _equity_per_share(ev + v.cash - v.bridge_debt - v.minority + v.investments, v.inputs or {}, v.fx,
                             v.shares)


def _solve(f, target: float, lo: float, hi: float) -> float | None:
    """The input between lo and hi at which f gives target, by bisection; None if none does."""
    try:
        a, b = f(lo), f(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if a is None or b is None or not (min(a, b) <= target <= max(a, b)):
        return None
    rising = b > a
    for _ in range(60):
        mid = (lo + hi) / 2
        try:
            got = f(mid)
        except (OverflowError, ZeroDivisionError):
            return None
        if got is None:
            return None
        if (got < target) == rising:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# How far the price's revenue growth sits above the company's reported record, in points.
GRADE_REASONABLE_PP, GRADE_AGGRESSIVE_PP = 3.0, 8.0


def grade(required: float | None, record: float | None) -> str | None:
    if required is None or record is None:
        return None
    gap = (required - record) * 100
    return ("conservative" if gap <= 0 else "reasonable" if gap <= GRADE_REASONABLE_PP
            else "aggressive" if gap <= GRADE_AGGRESSIVE_PP else "extreme")


def reverse_dcf(v: Valuation, data: MarketData) -> None:
    """What today's price assumes, one input at a time, the others at the base case."""
    price = data.price
    if not price or not v.inputs:
        return
    v.implied_margin = _solve(lambda m: _revalue(v, margin=m), price, -0.2, 0.9)
    if v.method == "perpetuity":
        v.implied_terminal = _solve(lambda g: _revalue(v, growth=g), price, -0.05,
                                    v.wacc - MIN_WACC_GROWTH_SPREAD - 1e-4)
        floor = (v.growth or 0.0) + MIN_WACC_GROWTH_SPREAD + 1e-4
    else:
        v.implied_terminal = _solve(lambda x: _revalue(v, multiple=x), price, 0.5, 100.0)
        floor = 0.02
    v.implied_wacc = _solve(lambda w: _revalue(v, wacc=w), price, max(floor, 0.02), 0.30)
    years = [rev for _, rev, _ in data.history or []
             if isinstance(rev, (int, float)) and rev > 0]
    if len(years) >= 2:
        v.record_cagr = (years[-1] / years[0]) ** (1 / (len(years) - 1)) - 1
    v.cagr_grade = grade(v.implied_cagr, v.record_cagr)
    margins = [y["ebit"] / y["revenue"] for y in v.reported
               if y.get("revenue") and y.get("ebit") is not None]
    if margins:
        v.record_margin = max(margins)
        v.margin_grade = grade(v.implied_margin, v.record_margin)
    # The same three solves at the Street's average target.
    target = data.analyst_target
    if target:
        v.street_margin = _solve(lambda m: _revalue(v, margin=m), target, -0.2, 0.9)
        if v.method == "perpetuity":
            v.street_terminal = _solve(lambda g: _revalue(v, growth=g), target, -0.05,
                                       v.wacc - MIN_WACC_GROWTH_SPREAD - 1e-4)
        else:
            v.street_terminal = _solve(lambda x: _revalue(v, multiple=x), target, 0.5, 100.0)
        v.street_wacc = _solve(lambda w: _revalue(v, wacc=w), target, max(floor, 0.02), 0.30)


def implied_revenue_cagr(v: Valuation, price: float | None) -> float | None:
    """The revenue CAGR to the final forecast year at which this model's margins, discount
    rate and terminal give `price`: what a price assumes, in the model's own terms. None
    when no growth between -50% and +150% a year gets there."""
    if not price or v.base_revenue <= 0 or not v.inputs:
        return None
    n = len(v.rows)

    def value(c: float) -> float | None:
        return _revalue(v, [v.base_revenue * (1 + c) ** (i + 1) for i in range(n)])

    lo, hi = -0.5, 1.5
    try:
        v_lo, v_hi = value(lo), value(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if v_lo is None or v_hi is None or not (min(v_lo, v_hi) <= price <= max(v_lo, v_hi)):
        return None
    rising = v_hi > v_lo
    for _ in range(60):
        mid = (lo + hi) / 2
        got = value(mid)
        if got is None:
            return None
        if (got < price) == rising:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# Two drivers whose product is revenue (subscribers x ARPU, units x price): NFLX's of
# 2026-10-01 gave 325mn subscribers at $13.9 a month for FY2025, $54.2bn a year, against
# $45.2bn reported, and 405mn at $16.6 for FY2030, $80.7bn against its own $67.1bn.
DRIVER_SCALES = (1e-6, 1e-3, 1.0, 1e3, 1e6)
DRIVER_PERIODS = (1, 4, 12)
DRIVER_BUILD_BAND = (0.67, 1.5)     # a product this close to revenue is meant to build it
DRIVER_BUILD_TOLERANCE = 0.10


def _growth_driver_warnings(drivers: dict, base_revenue: float, revenue: list[float],
                            years: list) -> list[str]:
    """A driver that states the revenue growth rate must agree with the revenue line.

    SPGI's model of 2026-10-02 had "Organic revenue growth" at 7.0% for FY2026 and revenue
    falling 5% from a base year that still held the business it had spun off; the prose
    quoted the 7%. A fall the forecast means is fine (NKE's of the same day); a model that
    says growth and computes a fall is not.
    """
    out = []
    for name, spec in (drivers or {}).items():
        if not isinstance(spec, dict) or "%" not in str(spec.get("unit", "")):
            continue
        if not GROWTH_DRIVER.search(str(name)):
            continue
        values = spec.get("values")
        if not isinstance(values, list) or len(values) != len(revenue):
            continue
        prev = base_revenue
        for year, stated, rev in zip(years, values, revenue):
            if isinstance(stated, (int, float)) and prev:
                actual = (rev / prev - 1) * 100
                if abs(actual - stated) > GROWTH_DRIVER_GAP_PP:
                    out.append(
                        f"driver \"{name}\" is {stated:g}% for {year}, but revenue in the model "
                        f"changes {actual:+.1f}% that year ({prev:,.0f} to {rev:,.0f}). If a "
                        "disposal or spin-off is the difference, set `base_revenue` to the "
                        "continuing business and declare `pro_forma_base`; otherwise make the "
                        "driver and the revenue line agree")
                    break
            prev = rev
    return out


def _driver_build_warnings(drivers: dict, base_revenue: float, revenue: list[float],
                           years: list, currency: str) -> list[str]:
    numeric, priced = {}, {}
    for name, spec in (drivers or {}).items():
        if not isinstance(spec, dict):
            continue
        unit = str(spec.get("unit") or "")
        money = UNIT_CURRENCY.fullmatch(unit.strip())
        # A price is a currency per unit ("USD"); a volume has no currency ("mn"). Two
        # amounts of money (CAT's segment revenues, FICO's two ARRs) do not multiply.
        priced[name] = ("price" if money and not money.group("scale") else
                        "money" if money or re.search(r"\b(?:USD|EUR|GBP|bn|tn)\b", unit)
                        or re.search(r"\b(?:GW|MW|TWh|GWh|MWh)\b", unit) else "volume")
        base, values = spec.get("base"), spec.get("values")
        if (isinstance(base, (int, float)) and base > 0 and isinstance(values, list)
                and len(values) == len(revenue)
                and all(isinstance(x, (int, float)) and x > 0 for x in values)):
            numeric[name] = (float(base), [float(x) for x in values])
    names = list(numeric)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if {priced[a], priced[b]} != {"price", "volume"}:
                continue
            (a0, av), (b0, bv) = numeric[a], numeric[b]
            ratio, k, period = min(((a0 * b0 * s * p / base_revenue, s * p, p)
                                    for s in DRIVER_SCALES for p in DRIVER_PERIODS),
                                   key=lambda t: abs(math.log(t[0])))
            if not DRIVER_BUILD_BAND[0] <= ratio <= DRIVER_BUILD_BAND[1]:
                continue
            points = [("base year", a0 * b0 * k, base_revenue)] + [
                (str(y), x * z * k, rev) for y, x, z, rev in zip(years, av, bv, revenue)]
            off = [(label, built, rev) for label, built, rev in points
                   if abs(built / rev - 1) > DRIVER_BUILD_TOLERANCE]
            if off:
                per = {1: "", 4: " x 4 quarters", 12: " x 12 months"}[period]
                shown = "; ".join(f"{label}: {built:,.0f} against {rev:,.0f}"
                                  for label, built, rev in off[:3])
                out.append(f"drivers `{a}` x `{b}`{per} should build revenue, but come to "
                           f"({currency} mn) {shown}. Make the drivers multiply out to the "
                           "revenue line (or to the segment they build, named as such), or "
                           "drop the one that does not belong")
    return out


# How far the first forecast year's cash conversion may run ahead of the last reported
# year's before the model has to show why.
FCF_MARGIN_JUMP_PP = 4.0
# Reported D&A (EBITDA less operating income) this many times the model's means the model
# leaves out an amortisation the market's EBITDA adds back.
DA_BASIS_FACTOR = 2.0
DA_BASIS_MIN_PP = 5.0


def ppe_rollforward(data: MarketData, base_label: str, revenue: list[float],
                    capex_pct: list[float]) -> tuple[list[float], str] | None:
    """D&A as a share of each forecast year's revenue from a PP&E roll-forward, and a note
    saying so, for a capex-heavy company; None for any other, or without the data.

    Depreciation is the company's reported depreciation over opening net PP&E (averaged
    over the years reported) times each year's opening PP&E, which then grows by capex
    less depreciation. Amortisation of acquired intangibles needs no capex to replace it,
    so it runs off in a straight line to nothing by the final year.
    """
    block = getattr(data, "prompt_block", "") or ""
    reported = {y["label"]: y for y in market_data.reported_years(block) if y.get("label")}
    balance = market_data.balance_years(block)
    base = reported.get(base_label)
    if not base or not base.get("revenue") or base.get("capex") is None:
        return None
    if abs(base["capex"]) / base["revenue"] < CAPEX_HEAVY_SHARE:
        return None
    ppe = (balance.get(base_label) or {}).get("ppe")
    if not ppe or ppe <= 0:
        return None
    rates = []
    for label, year in reported.items():
        prior = f"FY{int(label[2:]) - 1}" if label[2:].isdigit() else ""
        opening = (balance.get(prior) or {}).get("ppe")
        if year.get("depreciation") and opening and opening > 0:
            rates.append(abs(year["depreciation"]) / opening)
    if not rates:
        return None
    lo, hi = PPE_DEPRECIATION_RANGE
    rate = min(max(sum(rates[:3]) / len(rates[:3]), lo), hi)
    amortisation = abs(base.get("amortization") or 0.0)
    n = len(revenue)
    out = []
    for i in range(n):
        depreciation = rate * ppe
        ppe += revenue[i] * capex_pct[i] - depreciation
        da = depreciation + amortisation * (1 - (i + 1) / n)
        out.append(da / revenue[i] if revenue[i] else 0.0)
    note = (f"capex was {abs(base['capex']) / base['revenue']:.0%} of {base_label} revenue, so "
            f"D&A comes from a PP&E roll-forward: depreciation at {rate:.1%} of opening net "
            f"PP&E (the company's reported rate), "
            + (f"and {base_label} amortisation of intangibles "
               f"({data.reporting_currency} {amortisation:,.0f}mn) running off by "
               f"the final year; " if amortisation else "")
            + "`da_pct_revenue` is not used")
    return out, note


def _cash_conversion_warnings(v: Valuation, data: MarketData) -> list[str]:
    """The forecast's cash generation against the company's own record.

    NFLX's model of 2026-10-01 put D&A at 5% of revenue where the statements add back 37%
    (content amortisation), and capex at 4.5% where the cash spent on content is about 39%:
    its EBITDA was not the market's, so its "8.7x terminal against 8.3x today" compared two
    different things, and the reviewer could only guess at the size of the error.
    """
    reported = [y for y in market_data.reported_years(getattr(data, "prompt_block", "") or "")
                if y.get("revenue")]
    if not reported:
        return []
    out = []
    latest = reported[0]
    first = v.rows[0]
    rev = latest["revenue"]
    # A roll-forward D&A is the company's own depreciation; EBITDA less operating income
    # also holds impairments (BABA's FY2026: RMB 63bn of its 127bn).
    if (not v.da_from_ppe and latest.get("ebitda") is not None
            and latest.get("ebit") is not None and first.revenue):
        da_reported = (latest["ebitda"] - latest["ebit"]) / rev * 100
        da_model = first.da / first.revenue * 100
        if da_reported > DA_BASIS_FACTOR * da_model and da_reported - da_model > DA_BASIS_MIN_PP:
            out.append(
                f"the statements add back D&A of {da_reported:.0f}% of revenue in "
                f"{latest['label']} (EBITDA less operating income), and your model "
                f"{da_model:.1f}%: an amortisation that the market's EBITDA, today's multiple and "
                "the peers' all include is missing, so every EV/EBITDA comparison is on another "
                "basis. Put it in `da_pct_revenue` and the cash spent replacing it (content, "
                "capitalised software, acquired intangibles) in `capex_pct_revenue`")
    with_fcf = [y for y in reported if y.get("fcf") is not None]
    if with_fcf and first.revenue:
        margins = [y["fcf"] / y["revenue"] * 100 for y in with_fcf[:3]]
        # Reported free cash flow is after interest; the model's is before it. FICO's
        # 37% reported is about 52% before its $5.6bn of debt is serviced.
        after_tax_debt = (v.cost.cost_of_debt if v.cost else
                          ((data.risk_free or config.RISK_FREE_DEFAULT) + config.DEBT_SPREAD)
                          * (1 - config.WACC_TAX_RATE))
        interest = v.debt * after_tax_debt / rev * 100 if rev else 0.0
        model = first.fcf_margin * 100
        if model - (max(margins) + interest) > FCF_MARGIN_JUMP_PP:
            label = with_fcf[0]["label"]
            out.append(
                f"{first.label} free cash flow is {model:.1f}% of revenue against "
                f"{margins[0]:.1f}% reported for {label}"
                + (f" ({', '.join(f'{m:.1f}%' for m in margins[1:])} before)"
                   if len(margins) > 1 else "")
                + (f", about {interest:.1f}pp more before interest" if interest >= 0.5 else "")
                + ": a jump in cash conversion the first forecast year. Check D&A against capex "
                "and working capital against what the cash flow statement shows, or say in the "
                "text what lifts conversion this fast")
    return out


# --------------------------------------------------------------------------- returns and leverage

def is_financial(data: MarketData) -> bool:
    """Banks and insurers measure return on equity; their "invested capital" means nothing."""
    return valuation_route.route(data).method != "fcff"


# Net debt above this many times trailing EBITDA makes borrowing a risk the pitch argues
# in its own paragraph; below it, a sentence says why it is not one.
LEVERAGE_PARAGRAPH = 1.5
# The borrowing the EPS sensitivity tries: this many times trailing EBITDA, spent on
# buying back stock at today's price.
BUYBACK_TURNS = 1.0
# The return new capital earns after the forecast, unless the model argues otherwise: this
# many times the WACC, or the company's best reported ROIC if higher. A moat may be argued
# up to NEW_CAPITAL_RETURN_MAX times the WACC.
NEW_CAPITAL_RETURN_DEFAULT = 2.0
NEW_CAPITAL_RETURN_MAX = 3.0
NEW_CAPITAL_RETURN_ASSET_LIGHT = 5.0
# A ROIC above this says the capital base is near zero, not that each unit of capital earns
# that much: SPOT's net cash and prepaid subscriptions left FY2024 invested capital at EUR
# 153mn against EUR 1,078mn of NOPAT, a "1365%" ROIC its pitch then argued from.
ROIC_MEANINGFUL_MAX = 2.5
# Reinvestment may fall short of growth / return by this share of NOPAT before it is sent back.
REINVESTMENT_TOLERANCE = 0.02
# The growth forever an exit multiple is held to when the model gives no perpetuity rate.
SUSTAINABLE_GROWTH = 0.03


@dataclass
class CapitalYear:
    label: str
    invested_capital: float         # equity + debt - cash at the year end, reporting mn
    nopat: float
    roic: float | None              # NOPAT over the average of opening and closing capital
    roic_ex_goodwill: float | None = None
    reported: bool = False
    # Profitable on less than no tangible capital: goodwill exceeds the whole invested
    # capital (SPGI's USD 52.7bn against 47.9bn), so ROIC without it has no meaning.
    no_tangible_capital: bool = False
    # Profitable on capital near zero or below (ROIC_MEANINGFUL_MAX): `roic` is None.
    capital_near_zero: bool = False


@dataclass
class Returns:
    """Return on invested capital, reported and forecast, and what the terminal value
    assumes new capital earns."""
    years: list[CapitalYear]
    tax: float                      # the rate the reported years' operating income is taxed at
    goodwill: float | None          # goodwill and other intangibles, latest balance sheet
    terminal_growth: float | None   # the perpetuity's, or the growth an exit multiple implies
    terminal_reinvestment: float | None   # the share of NOPAT reinvested in the terminal year

    @property
    def terminal_roic(self) -> float | None:
        """growth = reinvestment rate x return on new capital, so the terminal value's
        growth and reinvestment fix the return it assumes on what it reinvests."""
        g, rate = self.terminal_growth, self.terminal_reinvestment
        if g is None or rate is None or g <= 0 or rate <= 0:
            return None
        return g / rate

    def year(self, label: str) -> CapitalYear | None:
        return next((y for y in self.years if _norm_year(y.label) == _norm_year(label)), None)


@dataclass
class Leverage:
    net_debt: float                     # bridge debt less cash, reporting mn
    net_debt_to_ebitda: float | None    # over the last four quarters' EBITDA
    interest_expense: float | None      # the last four quarters, reporting mn
    interest_coverage: float | None     # the last four quarters' EBIT over that interest
    cost_of_debt: float                 # after tax
    earnings_yield: float | None        # trailing net income over market cap
    buyback_amount: float | None        # BUYBACK_TURNS x trailing EBITDA, reporting mn
    buyback_eps_change: float | None    # what that borrowing does to EPS, as a fraction

    @property
    def needs_paragraph(self) -> bool:
        return self.net_debt_to_ebitda is not None and self.net_debt_to_ebitda > LEVERAGE_PARAGRAPH


def capital_returns(v: Valuation, data: MarketData) -> Returns | None:
    """ROIC from the reported balance sheets, carried through the forecast.

    Invested capital is the financing side, equity plus debt less cash, from the same
    statements as the bridge. Each forecast year adds what the model reinvests: capex less
    D&A plus working capital. Reported years tax operating income at the model's first-year
    rate. Banks and insurers get none, and a year whose capital is not positive (buybacks
    beyond equity) has no ROIC.
    """
    block = getattr(data, "prompt_block", "") or ""
    if is_financial(data):
        return None
    balance = market_data.balance_years(block)
    income = {y["label"]: y for y in market_data.reported_years(block)}
    tax = v.inputs.get("tax", [0.0])[0]

    def capital(label: str) -> float | None:
        b = balance.get(label, {})
        if b.get("equity") is None:
            return None
        return (b["equity"] + b.get("minority", 0.0) + b.get("debt", 0.0)
                - b.get("cash", 0.0))

    def roic(nopat: float, opening: float | None, closing: float | None) -> float | None:
        if opening is None or closing is None or (opening + closing) / 2 <= 0:
            return None
        value = nopat / ((opening + closing) / 2)
        return None if value > ROIC_MEANINGFUL_MAX else value

    def near_zero(nopat: float, opening: float | None, closing: float | None) -> bool:
        return (opening is not None and closing is not None and nopat > 0
                and roic(nopat, opening, closing) is None)

    labels = sorted(label for label in balance if capital(label) is not None)
    if not labels:
        return None
    base = _norm_year(v.base_label)
    start = next((l for l in reversed(labels) if _norm_year(l) <= base), labels[-1])
    years: list[CapitalYear] = []
    for prev, label in zip(labels, labels[1:]):
        ebit = income.get(label, {}).get("ebit")
        if ebit is None or _norm_year(label) > _norm_year(start):
            continue
        nopat = ebit * (1 - tax) if ebit > 0 else ebit
        opening, closing = capital(prev), capital(label)
        gw_open, gw_close = balance[prev].get("goodwill"), balance[label].get("goodwill")
        known = gw_open is not None and gw_close is not None
        ex = roic(nopat, opening - gw_open, closing - gw_close) if known else None
        negative = (known and nopat > 0
                    and (opening - gw_open + closing - gw_close) / 2 <= 0)
        years.append(CapitalYear(label, closing, nopat, roic(nopat, opening, closing), ex,
                                 reported=True, no_tangible_capital=negative,
                                 capital_near_zero=near_zero(nopat, opening, closing)))
    opening = capital(start)
    for r in v.rows:
        closing = opening + r.capex - r.da + r.nwc_change
        years.append(CapitalYear(r.label, closing, r.nopat, roic(r.nopat, opening, closing),
                                 capital_near_zero=near_zero(r.nopat, opening, closing)))
        opening = closing

    if v.method == "perpetuity":
        growth = v.growth
        last = v.glide_rows[-1] if v.glide_rows else v.rows[-1]
    else:
        growth, last = v.implied_growth, v.rows[-1]
    rate = (last.nopat - last.fcf) / last.nopat if last.nopat > 0 else None
    return Returns(years=years, tax=tax, goodwill=balance[start].get("goodwill"),
                   terminal_growth=growth, terminal_reinvestment=rate)


def _after_tax_debt_cost(v: Valuation, data: MarketData) -> float:
    if v.cost:
        return v.cost.cost_of_debt
    rf = data.risk_free if data.risk_free is not None else config.RISK_FREE_DEFAULT
    return (rf + config.DEBT_SPREAD) * (1 - config.WACC_TAX_RATE)


def leverage(v: Valuation, data: MarketData) -> Leverage | None:
    """Net debt against earnings, interest cover, and what borrowing would do to EPS.

    The EPS figure borrows BUYBACK_TURNS x trailing EBITDA at the after-tax cost of debt
    and retires stock at today's price: EPS rises when the earnings yield is above that
    cost and falls when it is below.
    """
    if not data.cash_debt_known:
        return None
    block = getattr(data, "prompt_block", "") or ""
    net_debt = v.bridge_debt - v.cash
    ebitda = v.ttm_ebitda if v.ttm_ebitda and v.ttm_ebitda > 0 else None
    interest = market_data.trailing_quarters(block, "Interest Expense")
    interest = abs(interest) if interest else None
    ebit = market_data.trailing_quarters(block, "Operating Income")
    kd = _after_tax_debt_cost(v, data)
    income = getattr(data, "ttm_net_income", None)
    market_cap = v.price * v.shares / v.fx / 1e6
    earnings_yield = income / market_cap if income is not None and market_cap > 0 else None

    amount = eps_change = None
    if ebitda and income and income > 0:
        amount = BUYBACK_TURNS * ebitda
        retired = amount * 1e6 * v.fx / v.price
        if retired < 0.9 * v.shares:
            eps_change = ((income - amount * kd) / (v.shares - retired)) / (income / v.shares) - 1
    return Leverage(net_debt=net_debt,
                    net_debt_to_ebitda=net_debt / ebitda if ebitda else None,
                    interest_expense=interest,
                    interest_coverage=(ebit / interest if interest and ebit is not None
                                       else None),
                    cost_of_debt=kd, earnings_yield=earnings_yield,
                    buyback_amount=amount, buyback_eps_change=eps_change)


def _returns_notes(v: Valuation) -> list[str]:
    """A terminal value that reinvests at a return below the WACC: growth destroying value."""
    r = v.returns
    if r is None or r.terminal_growth is None or r.terminal_growth <= 0:
        return []
    roic = r.terminal_roic
    if roic is not None and roic < v.wacc:
        what = "grows" if v.method == "perpetuity" else "is priced to grow"
        return [f"the terminal value {what} {r.terminal_growth:.1%} a year on a {roic:.1%} "
                f"return on the capital it reinvests, below the {v.wacc:.1%} WACC: growth "
                "there destroys value, so less reinvestment or lower growth would be worth more"]
    return []


def _terminal_row(v: Valuation) -> YearRow:
    """The year the terminal value repeats: the last glide year, or the final forecast year."""
    return v.glide_rows[-1] if v.method == "perpetuity" and v.glide_rows else v.rows[-1]


def new_capital_return(v: Valuation, terminal: dict) -> tuple[float, str, list[str]]:
    """The return the terminal value's new capital may earn: (return, how it was set, warnings).

    By default twice the WACC, or the best ROIC the company has reported if that is higher.
    The model may argue for more in `terminal.new_capital_return`, with the evidence in
    `new_capital_rationale`, up to three times the WACC or that best ROIC.
    """
    # New capital buys plant and working capital, not the goodwill of past acquisitions, so
    # an acquirer's ROIC without goodwill is the better guide to what it earns.
    reported = [max(x for x in (y.roic, y.roic_ex_goodwill) if x is not None)
                for y in (v.returns.years if v.returns else [])
                if y.reported and y.roic is not None]
    best = max(reported) if reported else 0.0
    default = max(NEW_CAPITAL_RETURN_DEFAULT * v.wacc, best)
    how = ("the company's best reported ROIC" if best > NEW_CAPITAL_RETURN_DEFAULT * v.wacc
           else f"{NEW_CAPITAL_RETURN_DEFAULT:.0f}x the WACC")
    raw = terminal.get("new_capital_return")
    if raw is None:
        return default, how, []
    warnings = []
    declared = _fraction(raw, "model.terminal.new_capital_return")
    # A business that earns its profit on less than no tangible capital may argue more: its
    # organic growth needs little beyond the R&D and data already in its costs.
    light = any(y.no_tangible_capital for y in (v.returns.years if v.returns else []))
    times = NEW_CAPITAL_RETURN_ASSET_LIGHT if light else NEW_CAPITAL_RETURN_MAX
    ceiling = max(times * v.wacc, best)
    if declared < v.wacc - RANGE_TOLERANCE or declared > ceiling + RANGE_TOLERANCE:
        warnings.append(f"`terminal.new_capital_return` of {declared:.1%} is outside "
                        f"{v.wacc:.1%} (the WACC) to {ceiling:.1%} ({times:.0f}x the WACC"
                        + (", as its goodwill exceeds its invested capital" if light else "")
                        + ", or the best reported ROIC); it was held to that range")
    declared = min(max(declared, v.wacc), ceiling)
    if len(str(terminal.get("new_capital_rationale") or "").split()) < 8:
        warnings.append("`terminal.new_capital_return` needs a `new_capital_rationale` giving "
                        "the evidence that new capital earns it: pricing power, a patent, a "
                        "network effect, or growth that is funded through R&D or content "
                        "rather than capex")
    return declared, "the model's", warnings


def _reinvestment_warnings(v: Valuation, terminal: dict, data: MarketData) -> list[str]:
    """Growth forever must be paid for: reinvestment = growth / return on new capital.

    In 10 of the 14 pitches saved by 2026-10-04 the terminal value grew every year while the
    final year's capex sat at or below D&A, so it reinvested nothing: NOK's 13x exit priced
    4.7% growth forever while releasing 31% of NOPAT, and its ROIC climbed from 3% to 22%.
    """
    if not config.REINVESTMENT_CHECK:
        return []
    if v.method == "perpetuity":
        g = v.growth
    else:
        g = v.implied_growth
    last = _terminal_row(v)
    if g is None or g <= 0 or last.nopat <= 0 or last.revenue <= 0:
        return []
    if is_financial(data):
        return []
    r, how, warnings = new_capital_return(v, terminal)
    v.new_capital_return = r
    rate = (last.nopat - last.fcf) / last.nopat
    needed = g / r
    if rate >= needed - REINVESTMENT_TOLERANCE:
        return warnings
    label = v.rows[-1].label
    da_pct = last.da / last.revenue
    declare = ("; or, if new capital earns more than that, set `terminal.new_capital_return` "
               "with a `new_capital_rationale` giving the evidence")
    reinvesting = (f"reinvests {rate:.0%} of NOPAT" if rate > 0 else
                   f"reinvests nothing (capex no higher than D&A and working capital release "
                   f"{-rate:.0%} of NOPAT)")
    # Customers who pay in advance can fund growth through working-capital releases:
    # FCF above NOPAT is not automatically a missing reinvestment (SPOT, 2026-10-11,
    # was blocked for keeping its prepaid subscriptions). This can hold even when capex
    # is below D&A; make the mechanism visible in the delivered pitch.
    if v.method == "perpetuity" and last.nwc_change < 0:
        v.notes.append(
            f"terminal growth of {g:.1%} would need {needed:.0%} of NOPAT reinvested at a "
            f"{r:.0%} return on new capital; the model reinvests {rate:.0%} because growth "
            "releases working capital (customers pay in advance), which is treated as a "
            "feature of the business")
        return warnings
    if v.method == "perpetuity":
        capex = (last.capex + (needed - rate) * last.nopat) / last.revenue
        top = rate * r
        warnings.append(
            f"the terminal value grows {g:.1%} a year forever but {reinvesting}. At a "
            f"{r:.0%} return on new capital ({how}) that growth needs {needed:.0%} of NOPAT "
            f"reinvested. Raise {label} `capex_pct_revenue` to about {capex:.1%} (D&A is "
            f"{da_pct:.1%}) so the asset base grows with the business, or lower "
            f"`terminal.growth` to {max(top, 0.0):.1%}" + declare)
        return warnings
    # An exit multiple fixes the terminal value, so more capex alone only raises the growth
    # it implies. The consistent pair is a sustainable growth rate, the reinvestment that
    # funds it, and the multiple those two give: NOPAT x (1 + g) x (1 - g / r) / (WACC - g).
    sustainable = v.growth if v.growth is not None and v.growth > 0 else min(g, SUSTAINABLE_GROWTH)
    share = sustainable / r
    capex = (last.capex + (share - rate) * last.nopat) / last.revenue
    basis = last.ebitda if v.exit_basis == "EBITDA" else last.ebit
    key = f"terminal.exit_ev_{v.exit_basis.lower()}"
    if basis > 0:
        multiple = (last.nopat * (1 + sustainable) * (1 - share)
                    / (v.wacc - sustainable) / basis)
        fix = (f"lower `{key}` toward about {multiple:.1f}x, the {v.exit_basis} multiple that "
               "growth and reinvestment support, or show from what comparable businesses earn "
               "on new capital why the market will pay more")
    else:
        fix = f"lower `{key}`"
    warnings.append(
        f"the {v.exit_multiple:.1f}x exit multiple prices {g:.1%} growth forever after {label}, "
        f"but the model {reinvesting} in {label}. At a {r:.0%} return on new capital ({how}) "
        f"that growth needs {needed:.0%} of NOPAT reinvested. {sustainable:.1%} growth forever "
        f"needs {share:.0%}: raise {label} `capex_pct_revenue` to about {capex:.1%} (D&A is "
        f"{da_pct:.1%}), and " + fix + declare)
    return warnings


def is_bound_warning(warning: str) -> bool:
    """Whether a valuation warning says a model input was invalid or constrained."""
    text = warning.lower()
    # The house rate comes from beta and the Treasury, not the model: re-asking cannot fix it.
    if text.startswith("house wacc"):
        return False
    if (("terminal value grows" in text or "exit multiple prices" in text)
            and "reinvest" in text and "needs" in text):
        # An unfunded terminal growth assumption is a model defect, not a note to ship.
        return True
    return any(term in text for term in (
        "wacc", "tax rate ", "exit multiple of ", "new_capital_return",
        "wacc_premium", "scenario values are out of order", "bull case comes out",
        "bear case comes out")) and any(term in text for term in (
            "outside", "held to", "out of order", "not above", "not below"))


def _apply_scenario(v: Valuation, model: dict, data: MarketData, name: str,
                    cost: CostOfCapital | None = None) -> None:
    """Price a bull or bear case by replacing inputs in the base forecast."""
    raw = model.get(name) if name == "bull" else (model.get("bear") or model.get("downside"))
    is_bull = name == "bull"
    prefix = "bull" if is_bull else "bear"
    if not isinstance(raw, dict):
        v.warnings.append(f"`model.{name}` must be an object of inputs changed from the base case")
        return
    unknown = [k for k in raw if k not in DOWNSIDE_KEYS and not k.endswith("rationale")]
    if unknown:
        v.warnings.append(f"`model.{name}` has key(s) the code does not read ("
                          + ", ".join(unknown) + "); use " + ", ".join(sorted(DOWNSIDE_KEYS)))
    if not any(k in DOWNSIDE_KEYS for k in raw):
        v.warnings.append(f"`model.{name}` changes no input, so it is not a distinct scenario")
        return

    alt = {k: val for k, val in model.items()
           if k not in ("bull", "bear", "downside", "scenario_probabilities")}
    changes: list[str] = []
    try:
        if raw.get("revenue_change") is not None:
            # One cut for every year, or one per year: NEE's bad case lost a merger that
            # only adds revenue from FY2028, and a flat -15% also cut FY2026-27.
            n = len(model.get("years") or [])
            cuts = _series(raw, "revenue_change", n, fraction=True)
            valid = all(-1 < c <= 0 for c in cuts) if not is_bull else all(c >= 0 for c in cuts)
            if not valid or not any(cuts):
                direction = "a cut between 0 and -1" if not is_bull else "an increase of 0 or more"
                raise ModelInputError(f"`model.{name}.revenue_change` must be {direction}, "
                                      "one number or one per forecast year")
            rev = model.get("revenue")
            if isinstance(rev, list):
                alt["revenue"] = [x * (1 + c) for x, c in zip(rev, cuts)]
            elif rev is not None:
                alt["revenue"] = [rev * (1 + c) for c in cuts]
            if isinstance(model.get("segments"), dict):
                alt["segments"] = {name: [x * (1 + c) for x, c in zip(values, cuts)]
                                   for name, values in model["segments"].items()}
            # A level difference from the base case, not an annual decline/growth rate.
            direction = "above" if is_bull else "below"
            if len(set(cuts)) == 1:
                changes.append(f"revenue {abs(cuts[0]):.0%} {direction} the base case in every "
                               "forecast year")
            else:
                years = model.get("years") or []
                changes.append(f"revenue {direction} the base case by " + ", ".join(
                    f"{abs(c):.0%} in {y}" for c, y in zip(cuts, years) if c))
        for key in ("ebit_margin", "capex_pct_revenue", "wacc"):
            if raw.get(key) is not None:
                alt[key] = raw[key]
        if raw.get("terminal") is not None:
            if not isinstance(raw["terminal"], dict):
                raise ModelInputError("`model.downside.terminal` must be an object")
            alt["terminal"] = {**(model.get("terminal") or {}), **raw["terminal"]}
        # A variant keeps the house rate unless it sets its own.
        d = compute(alt, data, v.stakes.get("quotes"),
                    cost=cost if raw.get("wacc") is None else None)
    except (ModelInputError, TypeError) as exc:
        v.warnings.append(f"the {name} case could not be computed: {exc}")
        return

    last = d.rows[-1]
    if raw.get("ebit_margin") is not None:
        changes.append(f"EBIT margin {last.ebit_margin:.1%} by {last.label}")
    if raw.get("capex_pct_revenue") is not None:
        changes.append(f"capex {last.capex / last.revenue:.1%} of revenue by {last.label}")
    if raw.get("wacc") is not None:
        changes.append(f"WACC {d.wacc:.1%}")
    if raw.get("terminal") is not None:
        changes.append(f"terminal growth {d.growth:.1%}" if d.method == "perpetuity"
                       else f"exit {d.exit_multiple:.1f}x {d.exit_basis}")
    v.scenario_terminal[prefix] = (("growth", d.growth) if d.method == "perpetuity"
                                   else ("multiple", d.exit_multiple))
    v.scenario_wacc[prefix] = d.wacc
    v.scenario_story[prefix] = " ".join(str(raw.get("rationale") or "").split())
    if is_bull:
        v.bull_per_share, v.bull_changes, v.bull_rows = d.per_share, changes, d.rows
    else:
        v.downside_per_share, v.downside_changes, v.downside_rows = d.per_share, changes, d.rows
    # Publish the inputs in placeholders so the prose describes what the code valued.
    dv: dict[str, str] = {}
    cuts = [r.revenue / b.revenue - 1 for r, b in zip(d.rows, v.rows) if b.revenue]
    if raw.get("revenue_change") is not None and cuts:
        for r, c in zip(d.rows, cuts):
            # The size of the cut, as the prose says it: "a 10% revenue haircut".
            dv[f"{prefix}_revenue_change:{r.label}"] = f"{c * 100:+.0f}%"
            if not is_bull:
                dv[f"downside_revenue_cut:{r.label}"] = f"{abs(c) * 100:.0f}%"
        if len({round(c, 4) for c in cuts}) == 1:
            dv[f"{prefix}_revenue_change"] = f"{cuts[0] * 100:+.0f}%"
            if not is_bull:
                dv["downside_revenue_cut"] = f"{abs(cuts[0]) * 100:.0f}%"
    for r in d.rows:
        dv[f"{prefix}_revenue:{r.label}"] = _money(r.revenue, v.reporting_currency)
        dv[f"{prefix}_ebit_margin:{r.label}"] = f"{r.ebit_margin:.1%}"
    # Both where the case has both, as the base case's {{g}} and {{exit_multiple}}: SPGI's
    # sheet rewrite cited {{downside_g}} on an exit-multiple model with a growth cross-check.
    if d.growth is not None:
        dv[f"{prefix}_g"] = f"{d.growth:.1%}"
    if d.exit_multiple is not None:
        dv[f"{prefix}_exit_multiple"] = f"{d.exit_multiple:.1f}x"
    dv[f"{prefix}_wacc"] = f"{d.wacc:.1%}"
    base_upside = v.upside_pct
    scenario_upside = (d.per_share / v.price - 1) * 100 if v.price else base_upside
    if abs(scenario_upside - base_upside) < 10:
        v.warnings.append(f"the {name} case is close to the base case in upside "
                          "assumptions; check that it represents a distinct scenario")
    if is_bull:
        v.bull_values = dv
        if d.per_share <= v.per_share:
            v.warnings.append(f"the bull case comes out at {d.per_share:,.2f}, not above the "
                              f"base case of {v.per_share:,.2f}; change inputs for the better")
    else:
        # Keep the pre-bull/bear placeholder names working for older prompt text.
        for key, value in list(dv.items()):
            if key.startswith("bear_"):
                dv["downside_" + key[len("bear_"):]] = value
        v.downside_values = dv
        if d.per_share >= v.per_share:
            v.warnings.append(f"the bear case comes out at {d.per_share:,.2f}, not below the "
                              f"base case of {v.per_share:,.2f}; change inputs for the worse")


# --------------------------------------------------------------------------- rendering

def _fmt_mn(v: float) -> str:
    return f"({abs(v):,.0f})" if v < 0 else f"{v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"({abs(v) * 100:.1f}%)" if v < 0 else f"{v * 100:.1f}%"


def dcf_table(v: Valuation) -> str:
    """Year-by-year free cash flow build, so the sum can be checked by eye."""
    unit = v.reporting_currency
    header = ["Year", "Revenue", "EBIT", "NOPAT", "D&A - Capex - NWC", "FCF", "PV of FCF"]
    lines = [f"All figures in {unit} mn, discounted at {v.wacc:.1%}.", "",
             "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for r in v.rows:
        reinvest = r.da - r.capex - r.nwc_change
        lines.append("| " + " | ".join([
            r.label, _fmt_mn(r.revenue), _fmt_mn(r.ebit), _fmt_mn(r.nopat), _fmt_mn(reinvest),
            _fmt_mn(r.fcf), _fmt_mn(r.pv_fcf)]) + " |")
    lines.append(f"| **Total** | | | | | | **{_fmt_mn(v.pv_fcf)}** |")
    if v.glide_rows:
        g = v.glide_rows
        today = v.rows[-1].discount_factor
        start = g[0].revenue / v.rows[-1].revenue - 1
        lines += ["", f"Then a {len(g)}-year glide, computed: revenue growth steps from "
                  f"{start:.1%} in {g[0].label} to the {v.growth:.1%} terminal rate by "
                  f"{g[-1].label}, at {v.rows[-1].label}'s margins.", "",
                  "| Year | Revenue | EBIT | FCF | PV of FCF |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {r.label} | {_fmt_mn(r.revenue)} | {_fmt_mn(r.ebit)} | {_fmt_mn(r.fcf)} | "
                  f"{_fmt_mn(r.pv_fcf * today)} |" for r in g]
    return "\n".join(lines)


def returns_table(v: Valuation) -> str:
    """ROIC by year, reported then forecast, against the WACC."""
    r = v.returns
    rep = v.reporting_currency
    ex = any(y.roic_ex_goodwill is not None or y.no_tangible_capital for y in r.years)
    header = ["Year", f"Invested capital ({rep} mn)", f"NOPAT ({rep} mn)", "ROIC",
              *(["ROIC excl. goodwill"] if ex else []), "ROIC less WACC"]

    def share(x: float | None, y: CapitalYear | None = None) -> str:
        if x is None and y is not None and y.capital_near_zero:
            return "n.m. (capital near zero)"
        return "n.m." if x is None else _fmt_pct(x)

    lines = [f"Invested capital is equity plus debt less cash at the year end, from the balance "
             "sheet; each forecast year adds its capex less D&A plus working capital. ROIC is "
             f"operating income after tax at {r.tax:.1%} over the average of the year's opening "
             f"and closing capital, against the {v.wacc:.1%} WACC.", "",
             "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for y in r.years:
        spread = (f"{(y.roic - v.wacc) * 100:+.1f}pp" if y.roic is not None else "n.m.")
        lines.append("| " + " | ".join([
            f"{y.label}{'A' if y.reported else 'F'}", _fmt_mn(y.invested_capital),
            _fmt_mn(y.nopat), share(y.roic, y),
            *([("n.m. (no tangible capital)" if y.no_tangible_capital
                 else share(y.roic_ex_goodwill)) if y.reported else "-"] if ex else []),
            spread]) + " |")
    g, rate, roic = r.terminal_growth, r.terminal_reinvestment, r.terminal_roic
    if g is not None and rate is not None:
        how = ("grows" if v.method == "perpetuity" else "is priced to grow")
        start = f"After {v.rows[-1].label} the terminal value {how} {g:.1%} a year while "
        if rate > 0:
            lines += ["", start + f"reinvesting {rate:.0%} of NOPAT"
                      + (f": new capital earns {roic:.1%}." if roic is not None else ".")]
        else:
            lines += ["", start + "reinvesting nothing: capex no higher than D&A and working "
                      f"capital release {-rate:.0%} of NOPAT, so its growth is assumed to need "
                      "no new capital."]
    return "\n".join(lines)


def leverage_table(v: Valuation) -> str:
    """Net debt against earnings, interest cover, and what borrowing would do to EPS."""
    lv, rep = v.leverage, v.reporting_currency
    rows = [("Net debt (debt less cash)", _money(lv.net_debt, rep),
             "net cash" if lv.net_debt < 0 else "")]
    if lv.net_debt_to_ebitda is not None:
        rows.append(("Net debt / EBITDA, last four quarters", f"{lv.net_debt_to_ebitda:.1f}x", ""))
    if lv.interest_expense is not None:
        rows.append(("Interest expense, last four quarters", _money(lv.interest_expense, rep), ""))
    if lv.interest_coverage is not None:
        rows.append(("Interest cover (EBIT / interest)", f"{lv.interest_coverage:.1f}x",
                     "last four quarters"))
    rows.append(("After-tax cost of debt", f"{lv.cost_of_debt:.1%}",
                 "the WACC's" if v.cost else "Treasury + spread"))
    if lv.earnings_yield is not None:
        rows.append(("Earnings yield (net income / market cap)", f"{lv.earnings_yield:.1%}",
                     "last four quarters"))
    if lv.buyback_eps_change is not None:
        after = (f"; net debt would be {lv.net_debt_to_ebitda + BUYBACK_TURNS:.1f}x EBITDA"
                 if lv.net_debt_to_ebitda is not None else "")
        rows.append((f"EPS change from borrowing {BUYBACK_TURNS:.1f}x EBITDA "
                     f"({_money(lv.buyback_amount, rep)}) to buy back stock at today's price",
                     f"{lv.buyback_eps_change * 100:+.1f}%",
                     ("earnings yield above the after-tax cost of debt"
                      if lv.buyback_eps_change > 0 else
                      "earnings yield below the after-tax cost of debt") + after))
    lines = ["| Item | Value | Note |", "| --- | --- | --- |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------- cross-checks

# A peer check needs at least this many peers with a usable multiple; one peer is an
# anecdote, not a market price.
MIN_PEERS = 2
# A DCF this far from what the peers' multiples imply is worth the reviewer's attention.
PEER_GAP_NOTE = 0.5


@dataclass
class PeerCheck:
    """The company valued on its peers' median multiples, from market data."""
    peers: list                      # market_data.PeerMultiples
    median_ev_ebitda: float | None
    median_pe: float | None
    value_ev_ebitda: float | None    # per share, trading currency
    value_pe: float | None

    @property
    def value(self) -> float | None:
        """The headline peer value: EV/EBITDA when there is one, else P/E."""
        return self.value_ev_ebitda if self.value_ev_ebitda is not None else self.value_pe


def peer_check(v: Valuation, data: MarketData, peers: list) -> PeerCheck | None:
    """Value the company at its peers' median EV/EBITDA and P/E on trailing figures.

    The DCF's second method used to be an exit multiple the model chose itself. This one
    comes from what the market pays today for the companies the model names as peers.
    """
    evs = [p.ev_ebitda for p in peers if p.ev_ebitda]
    pes = [p.pe for p in peers if p.pe]
    median_ev = statistics.median(evs) if len(evs) >= MIN_PEERS else None
    median_pe = statistics.median(pes) if len(pes) >= MIN_PEERS else None

    value_ev = None
    if median_ev and data.ttm_ebitda and data.ttm_ebitda > 0:
        # Peers' multiples are on their own total debt, finance arms included, so the
        # company's is too.
        equity = median_ev * data.ttm_ebitda + v.cash - v.debt - v.minority + v.investments
        value_ev = equity * 1e6 * v.fx / v.shares
    value_pe = None
    if median_pe and data.ttm_net_income and data.ttm_net_income > 0:
        value_pe = median_pe * data.ttm_net_income * 1e6 * v.fx / v.shares
    if value_ev is None and value_pe is None:
        return None
    return PeerCheck(peers=list(peers), median_ev_ebitda=median_ev, median_pe=median_pe,
                     value_ev_ebitda=value_ev, value_pe=value_pe)


def value_at(v: Valuation, wacc: float, growth: float | None = None,
             multiple: float | None = None) -> float | None:
    """The per-share value under another WACC and terminal assumption, same forecast."""
    if v.family == "equity":
        import valuation_equity
        return (valuation_equity.revalue(v, ke=wacc, growth=growth)
                if growth is not None else None)
    n = len(v.rows)
    last = v.rows[-1]
    path = (v.inputs or {}).get("wacc_path")
    pv = sum(r.fcf * _factor(i, wacc, path) for i, r in enumerate(v.rows))
    if growth is not None:
        if wacc - growth < 0.005:
            return None
        prev = v.rows[-2].revenue if n > 1 else v.base_revenue
        inp = v.inputs or {}
        tv, _ = continuing_value(last, prev, wacc, growth,
                                 inp["tax"][-1] if inp else 0.21,
                                 inp["nwc_pct"][-1] if inp else 0.0, config.GLIDE_YEARS)
    else:
        tv = (last.ebitda if v.exit_basis == "EBITDA" else last.ebit) * multiple
    ev = pv + tv * _factor(n - 1, wacc, path)
    return _equity_per_share(ev + v.cash - v.bridge_debt - v.minority + v.investments, v.inputs or {}, v.fx,
                             v.shares)


# --------------------------------------------------------------------------- Model Outputs
#
# The section in the order of the house report: 1a market and volume build, 1b
# definitions, 2 financials, 3 intrinsic valuation, 4 DCF build, 5 scenarios and checks,
# 6 returns on capital and leverage, 7 what would change my mind; the PDF adds 8, the
# review notes. Every figure is computed here from the model; where the model has no
# figure for a row, the row says "n.a." rather than going missing.

NA = "n.a."
REVIEW_SECTION = 8
# The writer's Definitions prose and assumptions, and the thresholds table, are placed in
# these slots by pitch.py, which holds the fact sheet they cite.
DEFINITIONS_SLOT = "<!-- slot:definitions -->"
THRESHOLDS_SLOT = "<!-- slot:thresholds -->"
# Grid steps around the base case: half a point of WACC and of terminal growth, a tenth
# of the exit multiple.
GRID_WACC_STEPS = (-0.01, -0.005, 0.0, 0.005, 0.01)
GRID_GROWTH_STEPS = (-0.01, -0.005, 0.0, 0.005, 0.01)
GRID_MULTIPLE_STEPS = (0.8, 0.9, 1.0, 1.1, 1.2)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _bn(x: float | None) -> str:
    """Millions of the reporting currency as billions, one decimal, negatives in brackets."""
    if not _is_num(x):
        return NA
    return f"({abs(x) / 1e3:,.1f})" if x < 0 else f"{x / 1e3:,.1f}"


def _rate(x: float | None) -> str:
    return _fmt_pct(x) if _is_num(x) else NA


def _signed(x: float | None) -> str:
    """A percentage change, signed, one decimal: '-22.5%'."""
    return f"{x:+.1f}%" if _is_num(x) else NA


def _count(x) -> str:
    if not _is_num(x):
        return NA if x is None else str(x)
    if x < 0:
        return f"({_count(-x)})"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.1f}"


def _per_unit(x: float | None) -> str:
    if not _is_num(x):
        return NA
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}"


def _price(x: float | None) -> str:
    return f"{x:,.2f}" if _is_num(x) else NA


def _growth(start, end, years: int) -> str:
    """Compound growth a year; costs are given negative, so it runs on their size."""
    if not (_is_num(start) and _is_num(end)) or years <= 0:
        return NA
    a, b = abs(start), abs(end)
    if a == 0 or (start < 0) != (end < 0):
        return NA
    return _fmt_pct((b / a) ** (1 / years) - 1)


def _pts(start, end, years: int) -> str:
    """The average change in a rate, in points a year."""
    if not (_is_num(start) and _is_num(end)) or years <= 0:
        return NA
    d = (end - start) * 100 / years
    return f"({abs(d):.1f} pts)" if d < -0.05 else f"{max(d, 0.0):.1f} pts"


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    clean = lambda c: str(c).replace("|", "/").replace("\n", " ")
    lines = ["| " + " | ".join(clean(c) for c in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(clean(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _year_digits(label: str) -> int | None:
    digits = _norm_year(label)
    return int(digits) if digits.isdigit() else None


def _short(label: str) -> str:
    y = _year_digits(label)
    return f"FY{y % 100:02d}" if y else str(label)


@dataclass
class _Columns:
    """History start, base year, forecast midpoint and final year, with the spans the
    four growth columns run over."""
    hist: dict | None
    base: dict
    mid: int
    labels: list[str]
    growth_labels: list[str]
    spans: list[tuple[str, str, int]]   # (from, to, years); from/to in hist|base|mid|final


def _columns(v: Valuation) -> _Columns:
    n = len(v.rows)
    mid = min(2, n - 1) if n > 1 else 0
    base_year = _year_digits(v.base_label)
    base = v.inputs.get("reported_base") or {"label": v.base_label}
    hist = None
    if base_year:
        older = [y for y in v.reported if (_year_digits(y["label"]) or base_year) < base_year]
        hist = older[0] if older else None
    labels = ([f"{hist['label']}A"] if hist else []) + [
        f"{v.base_label}A", f"{v.rows[mid].label}F", f"{v.rows[-1].label}F"]
    spans: list[tuple[str, str, int]] = []
    if hist:
        spans.append(("hist", "base", base_year - _year_digits(hist["label"])))
    spans += [("base", "mid", mid + 1), ("mid", "final", n - 1 - mid), ("base", "final", n)]
    names = {"hist": hist["label"] if hist else "", "base": v.base_label,
             "mid": v.rows[mid].label, "final": v.rows[-1].label}
    growth = [f"{_short(names[a])}-{_short(names[b])}" for a, b, _ in spans]
    return _Columns(hist, base, mid, labels, growth, spans)


def _line(c: _Columns, label: str, unit: str, values: dict, kind: str) -> list[str]:
    """One row: values by column (hist, base, mid, final) formatted for its kind, then
    growth (money, counts, prices) or points a year (rates) across the spans."""
    fmt = {"money": _bn, "rate": _rate, "count": _count, "per": _per_unit}[kind]
    cells = ([fmt(values.get("hist"))] if c.hist else []) + [
        fmt(values.get(k)) for k in ("base", "mid", "final")]
    for a, b, years in c.spans:
        if kind == "rate":
            cells.append(_pts(values.get(a), values.get(b), years))
        elif kind == "flat":
            cells.append(NA)
        else:
            cells.append(_growth(values.get(a), values.get(b), years))
    return [label, unit, *cells]


def _unit_scale(unit: str) -> float:
    """How many units one driver unit counts: '4.9' in 'mn' is 4.9 million."""
    u = str(unit or "").lower()
    if re.search(r"\bbn\b|billion", u):
        return 1e9
    if re.search(r"\bmn\b|\bm\b|million", u):
        return 1e6
    if re.search(r"thousand|'000|\b000s?\b|\bk\b", u):
        return 1e3
    return 1.0


def _driver_values(v: Valuation, spec) -> dict | None:
    """A driver's base, midpoint and final values, or None when it has no full path."""
    if not isinstance(spec, dict):
        return None
    values = spec.get("values")
    if not isinstance(values, list) or len(values) != len(v.rows):
        return None
    mid = min(2, len(v.rows) - 1)
    return {"base": spec.get("base"), "mid": values[mid], "final": values[-1],
            "all": values}


# A driver in money is a price or a balance, not a count of units: NVDA's of 2026-10-12
# divided Graphics sales by its "Graphics ASP ($)" and printed USD 64,165,714 "per unit".
MONEY_UNIT = re.compile(r"[$€£¥₹]|\b(?:USD|EUR|GBP|JPY|CNY|RMB|HKD|TWD|KRW|INR|CHF|CAD|AUD|"
                        r"DKK|SEK|NOK|ILS|BRL)\b", re.I)


def _volume_drivers(v: Valuation) -> dict[str, tuple[str, dict]]:
    """Segment name -> the volume driver that builds it."""
    out = {}
    for name, spec in v.drivers.items():
        if (isinstance(spec, dict) and spec.get("segment") in v.segments
                and "%" not in str(spec.get("unit", ""))
                and not MONEY_UNIT.search(f"{spec.get('unit', '')} {name}")
                and _driver_values(v, spec)):
            out.setdefault(str(spec["segment"]), (str(name), spec))
    return out


def _per_unit_values(v: Valuation, seg: str, spec: dict) -> dict:
    """Segment sales over the driver's volume: revenue per unit, reporting currency."""
    d = _driver_values(v, spec) or {}
    scale = _unit_scale(spec.get("unit", ""))
    mid = min(2, len(v.rows) - 1)
    sales = {"base": v.segment_base.get(seg), "mid": v.segments[seg][mid],
             "final": v.segments[seg][-1]}
    out = {}
    for k in ("base", "mid", "final"):
        units, s = d.get(k), sales[k]
        out[k] = s * 1e6 / (units * scale) if _is_num(units) and units and _is_num(s) else None
    return out


def volume_build_table(v: Valuation) -> str:
    c = _columns(v)
    rep = v.reporting_currency
    mid = c.mid
    header = ["Market and Volume Build", "", *c.labels[-3:], *c.growth_labels[-3:]]
    # The build starts at the base year: history before it has no segment split here.
    cols = _Columns(None, c.base, mid, c.labels[-3:], c.growth_labels[-3:],
                    [s for s in c.spans if s[0] != "hist"])
    rows = []
    volume = _volume_drivers(v)
    for seg, values in v.segments.items():
        sales = {"base": v.segment_base.get(seg), "mid": values[mid], "final": values[-1]}
        if seg in volume:
            name, spec = volume[seg]
            d = _driver_values(v, spec)
            rows.append(_line(cols, name, str(spec.get("unit") or ""), d, "count"))
            rows.append(_line(cols, f"{seg} sales per unit", rep,
                              _per_unit_values(v, seg, spec), "per"))
            rows.append(_line(cols, f"{seg} sales", f"{rep} bn", sales, "money"))
        else:
            rows.append(_line(cols, f"{seg} sales (projected directly)", f"{rep} bn", sales,
                              "money"))
    reported = v.inputs.get("reported_base") or {}
    total = {"base": reported.get("revenue", v.base_revenue), "mid": v.rows[mid].revenue,
             "final": v.rows[-1].revenue}
    rows.append(_line(cols, "**Total sales**", f"{rep} bn", total, "money"))
    note = ("" if v.segments else "\n\n*The model gives no segments, so total sales are "
            "projected directly.*")
    if v.revenue_build:
        note += ("\n\n*Sales are built in code: each line's volume times its price, summed, "
                 "rebuilding the base year within 2%.*")
        chains = revenue_build.chain_table(v.revenue_build["chains"], [r.label for r in v.rows])
        if chains:
            note += "\n\n" + chains
    margins = conglomerate.margins_table(v)
    if margins:
        note += "\n\n" + margins
    return _md_table(header, rows) + note


def definitions_markdown(v: Valuation) -> str:
    """One paragraph per driver and per directly projected segment, tagged by basis."""
    paras = []
    for name, spec in v.drivers.items():
        if not isinstance(spec, dict):
            continue
        text = " ".join(str(spec.get("definition") or "").split()) or "No definition given."
        basis = str(spec.get("basis") or "").lower()
        tag = f"[{basis}]" if basis in DRIVER_BASES else f"[{NA}]"
        paras.append(f"**{name}:** {text} {tag}")
    volume = _volume_drivers(v)
    for seg in v.segments:
        if seg not in volume:
            paras.append(f"**{seg}:** no volume measure drives it in the model, so its sales "
                         "are projected directly. [analyst]")
    return "\n\n".join(paras)


def _reported_values(y: dict | None) -> dict:
    """A reported year's income statement lines as the condensed statement shows them."""
    if not y:
        return {}
    rev, ebit, fcf = y.get("revenue"), y.get("ebit"), y.get("fcf")
    cogs = y.get("cogs")
    gp = y.get("gross_profit")
    if gp is None and _is_num(rev) and _is_num(cogs):
        gp = rev - cogs
    if cogs is None and _is_num(rev) and _is_num(gp):
        cogs = rev - gp
    capex = y.get("capex")
    da = (y["ebitda"] - ebit) if _is_num(y.get("ebitda")) and _is_num(ebit) else None
    ratio = lambda a: a / rev if _is_num(a) and _is_num(rev) and rev else None
    return {"revenue": rev, "cogs": -cogs if _is_num(cogs) else None, "gp": gp,
            "gm": ratio(gp), "opex": -(gp - ebit) if _is_num(gp) and _is_num(ebit) else None,
            "ebit": ebit, "om": ratio(ebit),
            "fcf_adj": fcf - ebit if _is_num(fcf) and _is_num(ebit) else None,
            "fcf": fcf, "fm": ratio(fcf),
            "opex_pct": ratio(gp - ebit) if _is_num(gp) and _is_num(ebit) else None,
            "capex_pct": ratio(abs(capex)) if _is_num(capex) else None,
            "da_pct": ratio(da)}


def _forecast_values(v: Valuation, i: int) -> dict:
    r = v.rows[i]
    gm = v.gross_margin[i] if v.gross_margin else None
    gp = r.revenue * gm if gm is not None else None
    return {"revenue": r.revenue, "cogs": -(r.revenue - gp) if gp is not None else None,
            "gp": gp, "gm": gm, "opex": -(gp - r.ebit) if gp is not None else None,
            "ebit": r.ebit, "om": r.ebit_margin, "fcf_adj": r.fcf - r.ebit, "fcf": r.fcf,
            "fm": r.fcf_margin,
            "opex_pct": (gp - r.ebit) / r.revenue if gp is not None and r.revenue else None,
            "capex_pct": r.capex / r.revenue if r.revenue else None,
            "da_pct": r.da / r.revenue if r.revenue else None}


def financials_markdown(v: Valuation) -> str:
    c = _columns(v)
    rep = v.reporting_currency
    unit = f"{rep} bn"
    by = {"hist": _reported_values(c.hist), "base": _reported_values(c.base),
          "mid": _forecast_values(v, c.mid), "final": _forecast_values(v, len(v.rows) - 1)}
    if not by["base"].get("revenue"):
        by["base"]["revenue"] = v.base_revenue
    pick = lambda key: {k: by[k].get(key) for k in by}

    rows = []
    for seg, values in v.segments.items():
        rows.append(_line(c, seg, unit, {"base": v.segment_base.get(seg),
                                         "mid": values[c.mid], "final": values[-1]}, "money"))
    for label, key, kind in (("**Total Sales**", "revenue", "money"),
                             ("Cost of Goods Sold", "cogs", "money"),
                             ("Gross Profit", "gp", "money"),
                             ("Gross Profit Margin", "gm", "rate"),
                             ("Operating Costs", "opex", "money"),
                             ("**Operating Profit**", "ebit", "money"),
                             ("Operating Profit Margin", "om", "rate"),
                             ("FCF Adjustments", "fcf_adj", "flat"),
                             ("**Free Cash Flow**", "fcf", "money"),
                             ("FCF Margin", "fm", "rate")):
        u = "%" if kind == "rate" else unit
        if kind == "flat":
            row = _line(c, label, u, pick(key), "money")
            row[2 + len(c.labels):] = [NA] * len(c.spans)
            rows.append(row)
        else:
            rows.append(_line(c, label, u, pick(key), kind))
    header = ["Condensed Income Statement", "", *c.labels, *c.growth_labels]
    out = _md_table(header, rows)

    metrics = []
    volume = {seg: name for seg, (name, _) in _volume_drivers(v).items()}
    for name, spec in v.drivers.items():
        d = _driver_values(v, spec)
        if d is None:
            continue
        is_rate = "%" in str(spec.get("unit", ""))
        if is_rate:
            d = {k: (x / 100 if _is_num(x) and abs(x) > 1.5 else x) for k, x in d.items()}
        metrics.append(_line(c, str(name), str(spec.get("unit") or ""), d,
                             "rate" if is_rate else "count"))
        seg = next((s for s, n in volume.items() if n == name), None)
        if seg:
            metrics.append(_line(c, f"{seg} sales per unit", rep,
                                 _per_unit_values(v, seg, spec), "per"))
    for label, key in (("Gross margin", "gm"), ("Operating costs as a % of sales", "opex_pct"),
                       ("Capital expenditure as a % of sales", "capex_pct"),
                       ("D&A as a % of sales", "da_pct")):
        metrics.append(_line(c, label, "%", pick(key), "rate"))
    out += "\n\n" + _md_table(["Key Metrics", "", *c.labels, *c.growth_labels], metrics)

    first, mid, last = v.rows[0].label, v.rows[c.mid].label, v.rows[-1].label
    note = (f"*A = reported, F = forecast. {mid} is the third forecast year; {last} is the end "
            f"of the explicit forecast, which runs from {first}. Growth columns show compound "
            "annual growth; for rates and margins they show the average change in points a "
            "year. Free cash flow is operating cash flow less capital expenditure in the "
            "history, and operating profit after tax plus depreciation and amortisation, less "
            "capital expenditure and working-capital investment, in the forecast; FCF "
            "adjustments are free cash flow less operating profit. Company-reported capex may "
            "use a different scope.*")
    if v.gross_margin is None:
        note += ("\n\n*The model gives no gross margin, so forecast cost of goods sold and "
                 "operating costs are n.a.*")
    if v.inputs.get("pro_forma_reason"):
        note += (f"\n\n*{v.base_label}A figures are reported consolidated results and include the "
                 "business that left after that year; forecast figures are the continuing "
                 "business after " + str(v.inputs["pro_forma_reason"]).strip().rstrip(".")
                 + f". The DCF starts from a model base revenue of {_bn(v.base_revenue)}bn.*")
    return out + "\n\n" + note


def _note(text: str) -> str:
    return f"<< {text}" if text else ""


def intrinsic_valuation_markdown(v: Valuation) -> str:
    cur, rep = v.currency, v.reporting_currency
    c = v.cost
    market_cap = v.price * v.shares / 1e9
    rows = [["Current Share Price", cur, _price(v.price), _note("market data")],
            ["No. of Shares", "# mn", f"{v.shares / 1e6:,.1f}",
             _note("market cap / price, all share classes" if v.shares_from_market_cap
                   else "market data")],
            ["Market Capitalisation", f"{cur} bn", f"{market_cap:,.1f}", ""]]
    if rep != cur:
        rows.append([f"{cur} per {rep}", "x", f"{v.fx:,.4f}",
                     _note(f"as of {v.fx_date}" if v.fx_date else "market data")])
    rows += [
        ["Risk-free Rate", "%", f"{c.risk_free:.2%}" if c else NA,
         _note(c.risk_free_source if c else "the writer set the discount rate")],
        ["Beta", "x", f"{c.beta:.2f}" if c else NA,
         _note(f"raw {c.beta_raw:.2f}; {c.beta_source}" if c else "")],
        ["Equity Risk Premium", "%", f"{c.erp:.1%}" if c else NA, ""],
        ["Cost of Equity", "%", f"{c.cost_of_equity:.1%}" if c else NA, ""],
        ["After-tax Cost of Debt", "%", f"{c.cost_of_debt:.1%}" if c else NA,
         _note(f"debt weight {c.debt_weight:.1%}" if c else "")],
    ]
    if c and c.premium:
        rows.append(["Company Risk Premium", "%", f"{c.premium:.1%}",
                     _note(c.premium_rationale)])
    rows.append(["Discount Rate (WACC)", "%", f"{v.wacc:.1%}", _note(c.bound if c else "")])
    rows.append(["Valuation Date", "", date.today().strftime("%b-%y"),
                 _note(f"first forecast year {v.rows[0].label}")])
    out = _md_table(["Market Assumptions", "Unit", "Value", ""], rows)

    last = v.rows[-1].label
    t = []
    ebitda_x = (f"; {v.terminal_ev_ebitda:.1f}x EBITDA" if v.terminal_ev_ebitda else "")
    if v.method == "perpetuity":
        glide = (f", after a {len(v.glide_rows)}-year glide to {v.glide_rows[-1].label}"
                 if v.glide_rows else "")
        t.append(["Perpetuity Growth", "%", f"{v.growth:.1%}",
                  _note(f"terminal value {rep} {_bn(v.terminal_value)}bn at {last}{glide}"
                        f"{ebitda_x}")])
    else:
        implied = (f"; implies {v.implied_growth:.1%} growth forever"
                   if v.implied_growth is not None else "")
        t.append([f"Exit EV/{v.exit_basis}", "x", f"{v.exit_multiple:.1f}x",
                  _note(f"terminal value {rep} {_bn(v.terminal_value)}bn at {last}{implied}")])
    if v.cross_check_per_share is not None:
        other = "exit multiple" if v.method == "perpetuity" else "perpetuity growth"
        t.append([f"Cross-check: {other}", cur, _price(v.cross_check_per_share),
                  _note(f"{_signed((v.cross_check_per_share / v.price - 1) * 100)} against the "
                        "price; given no weight")])
    peer_ev = v.peers.median_ev_ebitda if v.peers else None
    t.append(["Cross-check: Peer Median EV/EBITDA", "x",
              f"{peer_ev:.1f}x" if peer_ev else NA,
              _note(f"{cur} {_price(v.peers.value_ev_ebitda)} per share; given no weight"
                    if peer_ev and v.peers.value_ev_ebitda else "")])
    for e in v.events:
        t.append([f"Material event: {e.get('name')}", "", str(e.get('year') or NA),
                  _note(f"inside the forecast; the years after it run before the terminal "
                        f"value, which carries {last}'s margin")])
    out += "\n\n" + _md_table(["Terminal Assumptions", "Unit", "Value", "Implied value"], t)

    s = [[f"PV of FCF, {v.rows[0].label} to {last}", f"{rep} bn", _bn(v.pv_fcf), ""],
         ["PV of Terminal Value", f"{rep} bn", _bn(v.pv_terminal),
          _note(f"{v.terminal_share:.1%} of enterprise value")],
         [f"Implied Enterprise Value ({rep})", f"{rep} bn", _bn(v.enterprise_value), ""],
         ["(+) Cash and Short-term Investments", f"{rep} bn", _bn(v.cash), _note("market data")],
         ["(-) Borrowings", f"{rep} bn", _bn(-v.debt),
          _note(f"excludes {rep} {_bn(v.operating_leases)}bn of operating leases, whose cost "
                "is in operating profit" if v.operating_leases else "market data")]]
    if v.minority:
        s.append(["(-) Minority Interests", f"{rep} bn", _bn(-v.minority),
                  _note("outside owners' share of subsidiaries, at book value")])
    if v.stakes.get("book"):
        s.append(["(+) Long-term Equity Investments", f"{rep} bn", _bn(v.stakes["book"]),
                  _note("associates and other stakes, at book")])
    for x in v.stakes.get("listed", []):
        s.append([f"(+) Stake in {x['name']}", f"{rep} bn", _bn(x["value"]),
                  _note(f"{x['shares']:,.1f}mn shares of {x['ticker']} at {x['price']:,.2f} "
                        f"{rep}")])
    if v.finance_debt:
        s.append(["(+) Finance Arm's Debt, Funded by Its Receivables", f"{rep} bn",
                  _bn(v.finance_debt), _note(v.finance_debt_source or "model input")])
    s.append([f"Implied Equity Value ({rep})", f"{rep} bn", _bn(v.equity_value), ""])
    if rep != cur:
        s.append([f"(x) {cur} per {rep}", "x", f"{v.fx:,.4f}", ""])
        s.append([f"Implied Equity Value ({cur})", f"{cur} bn",
                  f"{v.equity_value * v.fx / 1e3:,.1f}", ""])
    raised = (v.inputs or {}).get("raised")
    if raised:
        s.append(["(+) Cash Raised to Fund the Burn", f"{rep} bn", _bn(raised),
                  _note(f"{v.inputs['new_shares'] / 1e6:,.1f}mn new shares at today's price")])
    if v.going_concern_per_share is not None:
        p, if_failed = v.inputs["failure"]
        s.append(["Going-concern Share Price", cur, _price(v.going_concern_per_share),
                  _note("the DCF if the company survives")])
        s.append(["Probability of Failure", "%", f"{p:.0%}",
                  _note(v.distress.get("failure_source", ""))])
        s.append(["Share Price if It Fails", cur, _price(if_failed), _note("model input")])
    s.append(["Implied Share Price, Base Case", cur, _price(v.per_share),
              _note(f"{_signed(v.upside_pct)} against the price"
                    + ("; survival-weighted" if v.going_concern_per_share is not None else ""))])
    if v.weighted_per_share is not None:
        p = v.scenario_probabilities
        s.append(["Probability-weighted Share Price", cur, _price(v.weighted_per_share),
                  _note(f"bull {p.get('bull', 0):.0%}, base {p.get('base', 0):.0%}, bear "
                        f"{p.get('bear', 0):.0%}")])
    else:
        s.append(["Probability-weighted Share Price", cur, NA,
                  _note("no bull and bear cases to weight")])
    s.append(["**Implied Upside**", "%", f"**{_signed(v.recommendation_upside)}**",
              _note(f"rating {v.published_rating}")])
    if v.stops:
        s.append(["Rating withheld", "", "NO RATING", _note("; ".join(v.stops))])
    out += "\n\n" + _md_table(["Summary of Outputs", "Unit", "Value", ""], s)
    return out


def _scenario_rows(v: Valuation, case: str) -> list[YearRow]:
    return {"bear": v.downside_rows, "base": v.rows, "bull": v.bull_rows}[case]


def scenarios_table(v: Valuation) -> str:
    cur, rep = v.currency, v.reporting_currency
    cases = ("bear", "base", "bull")
    n = len(v.rows)
    mid = min(2, n - 1)
    ml, fl = v.rows[mid].label, v.rows[-1].label

    def per(case, f):
        rows = _scenario_rows(v, case)
        return f(rows) if rows else NA

    value = {"bear": v.downside_per_share, "base": v.per_share, "bull": v.bull_per_share}
    story = [v.scenario_story.get(k) or NA for k in cases]
    probs = v.scenario_probabilities
    terminal = []
    for k in cases:
        kind, x = (("growth", v.growth) if v.method == "perpetuity"
                   else ("multiple", v.exit_multiple)) if k == "base" else \
            v.scenario_terminal.get(k, (None, None))
        terminal.append(NA if x is None else f"{x:.1%}" if kind == "growth" else f"{x:.1f}x")
    wacc = [_rate(v.wacc if k == "base" else v.scenario_wacc.get(k)) for k in cases]
    rows = [["What happens", "", *story],
            ["Probability", "%", *[f"{probs[k]:.0%}" if k in probs else NA for k in cases]],
            [f"Sales, {ml}", f"{rep} bn", *[per(k, lambda r: _bn(r[mid].revenue)) for k in cases]],
            [f"Sales, {fl}", f"{rep} bn", *[per(k, lambda r: _bn(r[-1].revenue)) for k in cases]],
            [f"Change in sales, {ml} to {fl}", "%",
             *[per(k, lambda r: _rate(r[-1].revenue / r[mid].revenue - 1)
                   if r[mid].revenue else NA) for k in cases]],
            [f"Operating margin, {ml}", "%", *[per(k, lambda r: _rate(r[mid].ebit_margin))
                                               for k in cases]],
            [f"Operating margin, {fl}", "%", *[per(k, lambda r: _rate(r[-1].ebit_margin))
                                               for k in cases]],
            ["Discount rate", "%", *wacc],
            ["Terminal growth" if v.method == "perpetuity" else f"Exit EV/{v.exit_basis}",
             "%" if v.method == "perpetuity" else "x", *terminal],
            ["**Value per share**", cur, *[f"**{_price(value[k])}**" if value[k] is not None
                                           else NA for k in cases]],
            ["Against the price", "%", *[_signed((value[k] / v.price - 1) * 100)
                                         if value[k] is not None else NA for k in cases]]]
    weighted = (f"{_price(v.weighted_per_share)} ({_signed(v.weighted_upside_pct)})"
                if v.weighted_per_share is not None else NA)
    rows.append(["Probability-weighted value", cur, "", weighted, ""])
    return _md_table(["Scenarios", "Unit", "Bear", "Base", "Bull"], rows)


def price_assumes_table(v: Valuation) -> str:
    fl = v.rows[-1].label
    perp = v.method == "perpetuity"
    term = (lambda x: _rate(x)) if perp else (lambda x: f"{x:.1f}x" if _is_num(x) else NA)
    rows = [[f"Sales growth a year to {fl}", "%", _rate(v.implied_cagr), v.cagr_grade or "",
             _rate(v.street_cagr) if v.street_target else NA, _rate(v.model_cagr)],
            ["Operating margin, every year", "%", _rate(v.implied_margin), v.margin_grade or "",
             _rate(v.street_margin) if v.street_target else NA,
             f"{_rate(v.rows[-1].ebit_margin)} in {fl}"],
            ["Perpetual growth" if perp else f"Exit EV/{v.exit_basis}", "%" if perp else "x",
             term(v.implied_terminal), "",
             term(v.street_terminal) if v.street_target else NA,
             term(v.growth if perp else v.exit_multiple)],
            ["Discount rate", "%", _rate(v.implied_wacc), "",
             _rate(v.street_wacc) if v.street_target else NA, _rate(v.wacc)]]
    out = _md_table(["What the Price Assumes", "Unit", "Today's price", "Against the record",
                     "Street target", "My base case"], rows)
    record = []
    if v.record_cagr is not None:
        record.append(f"reported sales growth of {_rate(v.record_cagr)} a year")
    if v.record_margin is not None:
        record.append(f"best reported operating margin of {_rate(v.record_margin)}")
    out += ("\n\n*Each figure is solved alone, the other inputs at the base case; n.a. where no "
            "value in the solver's range gives the price. Grades compare the required figure "
            "with the company's record" + (f" ({' and '.join(record)})" if record else "")
            + ". The margin row asks for one flat margin in every year.*")
    return out


def value_at_ebitda(v: Valuation, wacc: float, multiple: float) -> float | None:
    """The per-share value with the terminal value at an EV/EBITDA multiple of the final
    forecast year, same forecast, at another WACC."""
    n, last = len(v.rows), v.rows[-1]
    if last.ebitda <= 0:
        return None
    path = (v.inputs or {}).get("wacc_path")
    pv = sum(r.fcf * _factor(i, wacc, path) for i, r in enumerate(v.rows))
    ev = pv + last.ebitda * multiple * _factor(n - 1, wacc, path)
    return _equity_per_share(ev + v.cash - v.bridge_debt - v.minority + v.investments, v.inputs or {}, v.fx,
                             v.shares)


def _grid(v: Valuation, title: str, heads: list[str], cell, terms: list,
          bold: tuple | None) -> tuple[str, int, int]:
    waccs = [v.wacc + d for d in GRID_WACC_STEPS if v.wacc + d > 0.02]
    rows, cells, same = [], 0, 0
    base_rating = rating_for(v.upside_pct)
    for w in waccs:
        line = [f"WACC {w:.1%}"]
        for t in terms:
            x = cell(w, t)
            text = _price(x) if x is not None else "n.m."
            if x is not None:
                cells += 1
                same += rating_for((x / v.price - 1) * 100) == base_rating
            if bold and abs(w - v.wacc) < 1e-9 and abs(t - bold[1]) < 1e-9:
                text = f"**{text}**"
            line.append(text)
        rows.append(line)
    return _md_table([title, *heads], rows), cells, same


def sensitivity_markdown(v: Valuation) -> str:
    cur = v.currency
    perp = v.method == "perpetuity"
    g0 = v.growth if perp else min(max(v.implied_growth or 0.02, 0.0), v.wacc - 0.02)
    growths = [g0 + d for d in GRID_GROWTH_STEPS]
    first, cells1, same1 = _grid(
        v, f"Value per Share ({cur}): WACC by terminal growth"
        + ("" if perp else " (reference only)"), [f"{g:.1%}" for g in growths],
        lambda w, g: value_at(v, w, growth=g), growths, ("g", g0) if perp else None)
    if v.method == "exit_multiple" and v.exit_basis == "EBITDA":
        m0 = v.exit_multiple
    else:
        m0 = v.terminal_ev_ebitda
    out = first
    if m0:
        multiples = [m0 * k for k in GRID_MULTIPLE_STEPS]
        primary = v.method == "exit_multiple" and v.exit_basis == "EBITDA"
        second, cells2, same2 = _grid(
            v, f"Value per Share ({cur}): WACC by exit EV/EBITDA"
            + ("" if primary else " (reference only)"), [f"{m:.1f}x" for m in multiples],
            lambda w, m: value_at_ebitda(v, w, m), multiples, ("m", m0) if primary else None)
        out += "\n\n" + second
    else:
        primary = False
        out += "\n\n*WACC by exit EV/EBITDA: n.a., the final forecast year has no positive EBITDA.*"
    if perp or not primary:
        cells, same, which = cells1, same1, "first"
    else:
        cells, same, which = cells2, same2, "second"
    if cells:
        out += (f"\n\n*The rating is unchanged in {same / cells:.0%} of the cells of the "
                f"{which} grid; the base case is in bold.*")
    return out


def peers_markdown(v: Valuation) -> str:
    cur = v.currency
    pc = v.peers
    if pc is None:
        return "*Peers: n.a., the model names no peers with usable multiples.*"
    fmt = lambda x: f"{x:.1f}x" if x is not None else NA
    rows = [[f"{p.name} ({p.symbol})", fmt(p.ev_ebitda), fmt(p.pe)] for p in pc.peers]
    rows.append(["**Median**", f"**{fmt(pc.median_ev_ebitda)}**", f"**{fmt(pc.median_pe)}**"])
    up = lambda x: f"{_price(x)} ({_signed((x / v.price - 1) * 100)})" if x is not None else NA
    rows.append([f"Value per share at the median, on trailing figures ({cur})",
                 up(pc.value_ev_ebitda), up(pc.value_pe)])
    head = ["Peers (trailing multiples)", "EV/EBITDA", "P/E"]
    if pc.median_ev_ebitda is None and all(p.ev_ebitda is None for p in pc.peers):
        # Banks and insurers have no EBITDA: a column of n.a. says nothing.
        head, rows = head[::2], [r[::2] for r in rows]
    return _md_table(head, rows)


def checks_notes_markdown(v: Valuation) -> str:
    """The paragraph under the grids: each figure the model computed, in one place."""
    cur, rep = v.currency, v.reporting_currency
    parts = []
    if v.house_per_share is not None:
        up = (v.house_per_share / v.price - 1) * 100
        # Worded without "base case": consistency.check reads that label beside a price
        # as the writer's base-case value.
        parts.append(f"**Default cost of capital:** discounted at the house rate of "
                     f"{v.house_wacc:.1%} instead of {v.wacc:.1%}, the same forecast is worth "
                     f"{cur} {_price(v.house_per_share)} ({_signed(up)}), rated {rating_for(up)}.")
    else:
        parts.append(f"**Default cost of capital:** the forecast is discounted at the house "
                     f"rate of {v.wacc:.1%}." if v.cost else
                     "**Default cost of capital:** n.a.")
    if v.street_target:
        count = f" from {v.street_count} analysts" if v.street_count else ""
        parts.append(f"**Street average:** {cur} {_price(v.street_target)}{count} "
                     f"({_signed((v.street_target / v.price - 1) * 100)}).")
    else:
        parts.append("**Street average:** n.a.")
    lv = v.leverage
    if lv is not None:
        bits = [f"net {'cash' if lv.net_debt < 0 else 'debt'} of {_money(abs(lv.net_debt), rep)}"]
        if lv.net_debt_to_ebitda is not None:
            bits.append(f"{lv.net_debt_to_ebitda:.1f}x trailing EBITDA")
        if lv.interest_coverage is not None:
            bits.append(f"interest cover of {lv.interest_coverage:.0f}x")
        parts.append("**Balance sheet:** " + ", ".join(bits) + ".")
        parts.append("**Buy-back test:** " + (
            f"borrowing {BUYBACK_TURNS:.1f}x EBITDA to buy back shares would change earnings "
            f"per share by {lv.buyback_eps_change * 100:+.1f}% on trailing earnings."
            if lv.buyback_eps_change is not None else "n.a."))
    elif v.family != "equity":
        parts += ["**Balance sheet:** n.a.", "**Buy-back test:** n.a."]
    path = (v.inputs or {}).get("wacc_path")
    if path:
        parts.append("**Changing leverage:** WACC by year " + ", ".join(
            f"{r.label} {x:.1%}" for r, x in zip(v.rows, path))
            + "; the terminal value is discounted from the final year's.")
    if v.cycle:
        c = v.cycle
        parts.append(f"**Cycle:** operating margin {c['low']:.1%} to {c['high']:.1%} over "
                     f"{c['span']} (mean {c['mean']:.1%}); the base year's {c['base']:.1%} "
                     f"sits at {c['position']:.0%} of that range, the terminal year's "
                     f"{v.rows[-1].ebit_margin:.1%} at "
                     f"{(v.rows[-1].ebit_margin - c['low']) / (c['high'] - c['low']):.0%}."
                     if c['high'] > c['low'] else "**Cycle:** n.a.")
    if v.conviction:
        parts.append(f"**Conviction:** {v.conviction} ({'; '.join(v.conviction_reasons)}).")
    out = " ".join(parts)
    flags = "; ".join(v.notes) if v.notes else "none"
    return out + f"\n\n**Flags raised by the model:** {flags}."


def model_outputs_markdown(v: Valuation) -> str:
    if v.family == "equity":
        import valuation_equity
        return valuation_equity.outputs_markdown(v)
    # The PDF draws a chart where a marker comment sits (charts.py); anything else that
    # reads the body skips the comment.
    md = ["### 1a. Market and volume build (base case)", volume_build_table(v),
          "### 1b. Definitions", definitions_markdown(v), DEFINITIONS_SLOT,
          "### 2. Financials (base case)", "<!-- chart:revenue -->", financials_markdown(v),
          "### 3. Intrinsic Valuation (base case, then weighted)",
          intrinsic_valuation_markdown(v),
          "### 4. DCF Build", dcf_table(v),
          "### 5. Scenarios and checks", scenarios_table(v), "<!-- chart:valuation -->",
          price_assumes_table(v), sensitivity_markdown(v), peers_markdown(v),
          parts_markdown(v), checks_notes_markdown(v),
          "### 6. Returns on capital and leverage",
          ("#### Returns on Capital\n\n" + returns_table(v)) if v.returns
          else "*Returns on capital: n.a., the market data has no balance sheet.*",
          ("#### Leverage\n\n" + leverage_table(v)) if v.leverage
          else "*Leverage: n.a., cash and debt are unavailable from market data.*",
          "### 7. What would change my mind", THRESHOLDS_SLOT]
    return "\n\n".join(x for x in md if x)


# --------------------------------------------------------------------------- placeholders

def _money(v_mn: float, currency: str) -> str:
    """A reporting-currency amount in millions, written the way the prose writes it."""
    a = abs(v_mn)
    sign = "-" if v_mn < 0 else ""
    if a >= 1e6:
        return f"{sign}{currency} {a / 1e6:,.2f}tn"
    if a >= 1e3:
        return f"{sign}{currency} {a / 1e3:,.1f}bn"
    return f"{sign}{currency} {a:,.0f}mn"


UNIT_CURRENCY = re.compile(r"(?P<cur>[A-Z]{3}|US\$|\$)\s*(?P<scale>tn|bn|mn|k)?")


def with_unit(x, unit) -> str:
    """A driver's value with its unit. NEE's of 2026-09-26 read "$55bn base to 85.0 by
    FY2030" and "backlog expanding to 50.0" because the placeholder carried the bare number.
    """
    text = f"{x:,}" if isinstance(x, (int, float)) else str(x)
    unit = str(unit or "").strip()
    m = UNIT_CURRENCY.fullmatch(unit)
    if m:
        cur = m.group("cur")
        return (f"{cur}{text}" if cur.endswith("$") else f"{cur} {text}") + (m.group("scale") or "")
    if unit.lower() in ("%", "x", "tn", "bn", "mn", "k"):
        return text + unit
    return f"{text} {unit}" if unit else text


def _common_placeholders(v: Valuation) -> dict[str, str]:
    """What the equity method publishes beside its own: price, value, rating, the
    discount rate, scenarios, peers. The enterprise DCF's are in `placeholders`."""
    cur = v.currency
    values = {
        "price": f"{cur} {v.price:,.2f}",
        "intrinsic": f"{cur} {v.recommendation_value:,.2f}",
        "upside": f"{pct(v.recommendation_upside)}",
        "base_intrinsic": f"{cur} {v.per_share:,.2f}",
        "base_upside": f"{pct(v.upside_pct)}",
        "rating": v.rating,
        "wacc": f"{v.wacc:.1%}",
        **({"wacc_premium": f"{v.cost.premium:.1%}"} if v.cost and v.cost.premium else {}),
        **({"street_target": f"{cur} {v.street_target:,.2f}"} if v.street_target else {}),
        **({"wacc_build": v.cost.describe(), "beta": f"{v.cost.beta:.2f}",
            "risk_free": f"{v.cost.risk_free:.1%}"} if v.cost else {}),
        "shares": f"{v.shares / 1e6:,.1f}mn",
    }
    if v.reporting_currency != cur:
        values["fx_rate"] = f"{v.fx:,.4f} {cur} per {v.reporting_currency}"
    if v.house_per_share is not None:
        values["house_intrinsic"] = f"{cur} {v.house_per_share:,.2f}"
        values["house_wacc"] = f"{v.house_wacc:.1%}"
    if v.conviction:
        values["conviction"] = v.conviction
    if v.stops:
        for key in ("intrinsic", "upside", "base_intrinsic", "base_upside"):
            values[key] = "withheld"
        values["rating"] = "NO RATING"
    if v.downside_per_share is not None:
        values["downside"] = values["bear"] = f"{cur} {v.downside_per_share:,.2f}"
        values["downside_upside"] = values["bear_upside"] = f"{pct(v.downside_pct)}"
        values.update(v.downside_values)
    if v.bull_per_share is not None:
        values["bull"] = f"{cur} {v.bull_per_share:,.2f}"
        values["bull_upside"] = f"{pct(v.bull_pct)}"
        values.update(v.bull_values)
    for name, probability in v.scenario_probabilities.items():
        values[f"{name}_probability"] = f"{probability:.0%}"
    if v.weighted_per_share is not None:
        values["weighted_intrinsic"] = f"{cur} {v.weighted_per_share:,.2f}"
        values["weighted_upside"] = pct(v.weighted_upside_pct)
    if v.peers and v.peers.median_pe is not None:
        values["peer_pe"] = f"{v.peers.median_pe:.1f}x"
    if v.peers and v.peers.value is not None:
        values["peer_value"] = f"{cur} {v.peers.value:,.2f}"
        values["peer_upside"] = f"{pct((v.peers.value / v.price - 1) * 100)}"
    return values


def placeholders(v: Valuation) -> dict[str, str]:
    """Values the prose may cite by {{name}} instead of doing the arithmetic itself."""
    if v.family == "equity":
        import valuation_equity
        return valuation_equity.placeholders(v)
    cur, rep = v.currency, v.reporting_currency
    values = {
        "price": f"{cur} {v.price:,.2f}",
        "intrinsic": f"{cur} {v.recommendation_value:,.2f}",
        "upside": f"{pct(v.recommendation_upside)}",
        # The base-case DCF on its own: what the bridge's equity over shares comes to.
        "base_intrinsic": f"{cur} {v.per_share:,.2f}",
        "base_upside": f"{pct(v.upside_pct)}",
        "rating": v.rating,
        "wacc": f"{v.wacc:.1%}",
        **({"wacc_premium": f"{v.cost.premium:.1%}"} if v.cost and v.cost.premium else {}),
        **({"street_target": f"{cur} {v.street_target:,.2f}"} if v.street_target else {}),
        **({"wacc_build": v.cost.describe(), "beta": f"{v.cost.beta:.2f}",
            "cost_of_equity": f"{v.cost.cost_of_equity:.1%}",
            "risk_free": f"{v.cost.risk_free:.1%}"} if v.cost else {}),
        "ev": _money(v.enterprise_value, rep),
        "equity": _money(v.equity_value, rep),
        "pv_fcf": _money(v.pv_fcf, rep),
        "tv": _money(v.terminal_value, rep),
        "pv_tv": _money(v.pv_terminal, rep),
        "tv_share": f"{v.terminal_share:.0%}",
        "cash": _money(v.cash, rep),
        "debt": _money(v.debt, rep),
        **({"minority_interest": _money(v.minority, rep)} if v.minority else {}),
        **({"finance_debt": _money(v.finance_debt, rep),
            "bridge_debt": _money(v.bridge_debt, rep)} if v.finance_debt else {}),
        "shares": f"{v.shares / 1e6:,.1f}mn",
    }
    if rep != cur:
        values["fx_rate"] = f"{v.fx:,.4f} {cur} per {rep}"
    if v.growth is not None:
        values["g"] = f"{v.growth:.1%}"
    if v.exit_multiple is not None:
        values["exit_multiple"] = f"{v.exit_multiple:.1f}x"
    if v.terminal_ev_ebitda:
        values["tv_ev_ebitda"] = f"{v.terminal_ev_ebitda:.1f}x"
    if v.method == "exit_multiple" and v.implied_growth is not None:
        values["tv_implied_growth"] = f"{v.implied_growth:.1%}"
    if v.current_ev_ebitda:
        values["ev_ebitda"] = f"{v.current_ev_ebitda:.1f}x"
    if v.model_cagr is not None:
        values["model_cagr"] = f"{v.model_cagr:.1%}"
    if v.implied_cagr is not None:
        values["implied_cagr"] = f"{v.implied_cagr:.1%}"
    if v.street_cagr is not None:
        values["street_cagr"] = f"{v.street_cagr:.1%}"
    if v.implied_margin is not None:
        values["implied_margin"] = f"{v.implied_margin:.1%}"
    if v.implied_wacc is not None:
        values["implied_wacc"] = f"{v.implied_wacc:.1%}"
    if v.implied_terminal is not None:
        values["implied_terminal"] = (f"{v.implied_terminal:.1%}" if v.method == "perpetuity"
                                      else f"{v.implied_terminal:.1f}x")
    if v.record_cagr is not None:
        values["record_cagr"] = f"{v.record_cagr:.1%}"
    if v.cagr_grade:
        values["cagr_grade"] = v.cagr_grade
    if v.house_per_share is not None:
        values["house_intrinsic"] = f"{cur} {v.house_per_share:,.2f}"
        values["house_wacc"] = f"{v.house_wacc:.1%}"
    if v.conviction:
        values["conviction"] = v.conviction
    if v.stops:
        for key in ("intrinsic", "upside", "base_intrinsic", "base_upside"):
            values[key] = "withheld"
        values["rating"] = "NO RATING"
    if v.downside_per_share is not None:
        values["downside"] = f"{cur} {v.downside_per_share:,.2f}"
        values["downside_upside"] = f"{pct(v.downside_pct)}"
        values["bear"] = values["downside"]
        values["bear_upside"] = values["downside_upside"]
        values.update(v.downside_values)
    if v.bull_per_share is not None:
        values["bull"] = f"{cur} {v.bull_per_share:,.2f}"
        values["bull_upside"] = f"{pct(v.bull_pct)}"
        values.update(v.bull_values)
    if v.scenario_probabilities:
        for name, probability in v.scenario_probabilities.items():
            values[f"{name}_probability"] = f"{probability:.0%}"
    if v.weighted_per_share is not None:
        values["weighted_intrinsic"] = f"{cur} {v.weighted_per_share:,.2f}"
        values["weighted_upside"] = pct(v.weighted_upside_pct)
    if v.cross_check_per_share is not None:
        values["cross_check"] = f"{cur} {v.cross_check_per_share:,.2f}"
        values["cross_check_upside"] = f"{pct((v.cross_check_per_share / v.price - 1) * 100)}"
    if v.peers:
        pc = v.peers
        if pc.median_ev_ebitda is not None:
            values["peer_ev_ebitda"] = f"{pc.median_ev_ebitda:.1f}x"
        if pc.median_pe is not None:
            values["peer_pe"] = f"{pc.median_pe:.1f}x"
        if pc.value is not None:
            values["peer_value"] = f"{cur} {pc.value:,.2f}"
            values["peer_upside"] = f"{pct((pc.value / v.price - 1) * 100)}"
    # The model's own inputs, by year, which the prose explains: SPGI's fourth facts-mode
    # rewrite cited {{da_pct_revenue}} and {{capex_pct_revenue}}, which did not exist, and
    # typed the rest. A plain name too where the input is the same every year.
    inputs: dict[str, list[str]] = {}
    prev = v.base_revenue
    for r in v.rows:
        growth = r.revenue - prev
        series = {"da_pct_revenue": r.da / r.revenue if r.revenue else None,
                  "capex_pct_revenue": r.capex / r.revenue if r.revenue else None,
                  "tax_rate": 1 - r.nopat / r.ebit if r.ebit else None,
                  "nwc_pct_revenue_change": r.nwc_change / growth if growth else None,
                  "revenue_growth": growth / prev if prev else None}
        for name, x in series.items():
            if x is not None:
                values[f"{name}:{r.label}"] = f"{x:.1%}"
                inputs.setdefault(name, []).append(f"{x:.1%}")
        prev = r.revenue
    for name, seen in inputs.items():
        if len(seen) == len(v.rows) and len(set(seen)) == 1:
            values[name] = seen[0]
    for r in v.rows:
        values[f"revenue:{r.label}"] = _money(r.revenue, rep)
        values[f"ebit:{r.label}"] = _money(r.ebit, rep)
        values[f"ebitda:{r.label}"] = _money(r.ebitda, rep)
        values[f"ebit_margin:{r.label}"] = f"{r.ebit_margin:.1%}"
        values[f"fcf:{r.label}"] = _money(r.fcf, rep)
        values[f"fcf_margin:{r.label}"] = f"{r.fcf_margin:.1%}"
    # Segments and drivers by name, the way the model reaches for them unprompted
    # ("{{segments.Ad-Supported:FY2030}}"); the pitch code folds the plural and the case.
    for name, series in v.segments.items():
        before = None       # the model gives no base-year segment revenue
        for r, amount in zip(v.rows, series):
            values[f"segment.{name}:{r.label}"] = _money(amount, rep)
            # Its growth that year: SPGI's rewrite of 2026-10-03 typed "(6.5%)".
            if before:
                values[f"segment_growth.{name}:{r.label}"] = f"{amount / before - 1:.1%}"
            before = amount
            # Its share of the year's revenue: SPGI's fifth rewrite typed "c.36%".
            if r.revenue:
                values[f"segment_share.{name}:{r.label}"] = f"{amount / r.revenue:.0%}"
        # A segment's revenue growth over the forecast, which the prose reaches for and had
        # no placeholder: SPGI's facts-mode rewrite of 2026-10-02 typed "a 4.8% CAGR". From
        # the base year when the model gives the segment there, else from the first forecast.
        base = v.segment_base.get(name)
        if base is not None:
            values[f"segment.{name}:{v.base_label}"] = _money(base, rep)
            # Friendly alias for the base-year input. The JSON model calls this
            # `segment_base`, and writers naturally reach for that spelling too.
            values[f"segment_base.{name}"] = _money(base, rep)
        start, span = (base, len(series)) if base and base > 0 else (series[0], len(series) - 1)
        if span > 0 and start > 0 and series[-1] > 0:
            values[f"segment_cagr.{name}"] = f"{(series[-1] / start) ** (1 / span) - 1:.1%}"
    for name, spec in v.drivers.items():
        series = spec.get("values") if isinstance(spec, dict) else None
        if isinstance(series, list) and len(series) == len(v.rows):
            for r, x in zip(v.rows, series):
                values[f"driver.{name}:{r.label}"] = with_unit(x, spec.get("unit"))
            # The base year's value, which SPOT's of 2026-10-07 could not cite and replaced
            # with FY2026's (315mn subscribers "today" against 290mn reported), and the
            # driver's own growth: its "11.1% CAGR" of subscribers was Premium revenue's.
            base = spec.get("base")
            if isinstance(base, (int, float)):
                values[f"driver.{name}:{v.base_label}"] = with_unit(base, spec.get("unit"))
                last = series[-1]
                if (base > 0 and isinstance(last, (int, float)) and last > 0
                        and "%" not in str(spec.get("unit", ""))):
                    values[f"driver_cagr.{name}"] = (
                        f"{(last / base) ** (1 / len(series)) - 1:.1%}")
    if v.investments:
        values["investments"] = _money(v.investments, rep)
    if v.parts:
        values["sotp_value"] = f"{cur} {v.parts['per_share']:,.2f}"
        values["sotp_upside"] = pct((v.parts["per_share"] / v.price - 1) * 100)
        for r in v.parts["rows"]:
            values[f"part.{r['name']}"] = _money(r["value"], rep)
    for x in v.stakes.get("listed", []):
        values[f"stake.{x['name']}"] = _money(x["value"], rep)
    if v.cycle:
        values["cycle_mean_margin"] = f"{v.cycle['mean']:.1%}"
        values["cycle_low_margin"] = f"{v.cycle['low']:.1%}"
        values["cycle_high_margin"] = f"{v.cycle['high']:.1%}"
        values["cycle_position"] = f"{v.cycle['position']:.0%}"
    if v.returns:
        rt = v.returns
        for y in rt.years:
            values[f"invested_capital:{y.label}"] = _money(y.invested_capital, rep)
            if y.roic is not None:
                values[f"roic:{y.label}"] = f"{y.roic:.1%}"
            if y.roic_ex_goodwill is not None:
                values[f"roic_ex_goodwill:{y.label}"] = f"{y.roic_ex_goodwill:.1%}"
        if rt.goodwill is not None:
            values["goodwill"] = _money(rt.goodwill, rep)
        if rt.terminal_roic is not None:
            values["terminal_roic"] = f"{rt.terminal_roic:.1%}"
        if rt.terminal_reinvestment is not None:
            values["terminal_reinvestment"] = f"{rt.terminal_reinvestment:.0%}"
    if v.new_capital_return is not None:
        values["new_capital_return"] = f"{v.new_capital_return:.1%}"
        # SPGI's writer of 2026-10-05 cited it by its path in the model.
        values["terminal_new_capital_return"] = values["new_capital_return"]
    if v.leverage:
        lv = v.leverage
        values["net_debt"] = _money(lv.net_debt, rep)
        if lv.net_debt < 0:
            values["net_cash"] = _money(-lv.net_debt, rep)
        values["cost_of_debt"] = f"{lv.cost_of_debt:.1%}"
        for name, x in (("net_debt_to_ebitda", lv.net_debt_to_ebitda),
                        ("interest_coverage", lv.interest_coverage)):
            if x is not None:
                values[name] = f"{x:.1f}x"
        if lv.interest_expense is not None:
            values["interest_expense"] = _money(lv.interest_expense, rep)
        if lv.earnings_yield is not None:
            values["earnings_yield"] = f"{lv.earnings_yield:.1%}"
        if lv.buyback_eps_change is not None:
            values["buyback_amount"] = _money(lv.buyback_amount, rep)
            values["buyback_eps_change"] = f"{lv.buyback_eps_change * 100:+.1f}%"
    return values
