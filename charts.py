"""Charts for the PDF: vector drawings from the computed valuation and market data.

Up to three, all in Model Outputs, both with the exact figures in the tables beside them:

- revenue and EBITDA, reported years then the forecast, so the growth path can be judged
  against what the business has already done;
- the valuation range: the DCF (with its sensitivity range), the cross-check, the
  downside case, the peer values and the Street average against the current price;
- for a bank or insurer, ROE by year against the cost of equity and the ROE the price
  assumes.

Colours are the first two slots of a colour-blind-checked palette (blue, orange: adjacent
CVD delta E 24.7 on white). Forecast bars are a lighter step of the same hue, and a
labelled divider says where the forecast starts, so colour never carries that alone.
"""

from __future__ import annotations

import logging
import math
import re

from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String
from reportlab.lib import colors

from market_data import MarketData
from valuation import Valuation, pct, value_at

log = logging.getLogger("stockbot.charts")

# `<!-- chart:revenue -->` in the body marks where a chart goes.
MARKER = re.compile(r"^<!--\s*chart:(\w+)\s*-->$")

BLUE = colors.HexColor("#2a78d6")
BLUE_LIGHT = colors.HexColor("#9ec5f4")
BLUE_RANGE = colors.HexColor("#cde2fb")
ORANGE = colors.HexColor("#eb6834")
ORANGE_LIGHT = colors.HexColor("#f6b89c")
INK = colors.HexColor("#0b0b0b")
INK_2 = colors.HexColor("#52514e")
MUTED = colors.HexColor("#898781")
GRID = colors.HexColor("#e1e0d9")
AXIS = colors.HexColor("#c3c2b7")
SURFACE = colors.white
FONT, BOLD = "Helvetica", "Helvetica-Bold"


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Round tick values covering lo..hi: 0 / 10 / 20, never 13.7."""
    span = hi - lo or abs(hi) or 1.0
    raw = span / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.1f}" if abs(value) >= 10 \
        else f"{value:,.2f}"


def _tick_fmt(ticks: list[float]):
    """One format for a whole axis, with no more decimals than its step needs."""
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    places = next((p for p in (0, 1, 2) if abs(step * 10 ** p - round(step * 10 ** p)) < 1e-6),
                  2)
    return lambda t: f"{t:,.{places}f}"


def _label(d: Drawing, x: float, y: float, text: str, size: float, fill, font=FONT,
           anchor: str = "start") -> None:
    """Text on a surface-coloured backing, so a gridline never runs through it."""
    w = size * 0.52 * len(text)
    left = x if anchor == "start" else x - w if anchor == "end" else x - w / 2
    d.add(Rect(left - 1.5, y - 1.5, w + 3, size + 1.5, fillColor=SURFACE, strokeColor=None))
    d.add(String(x, y, text, fontName=font, fontSize=size, fillColor=fill, textAnchor=anchor))


def _column(d: Drawing, x: float, base: float, top: float, width: float, fill) -> None:
    """A column with a rounded data end and a square foot on the baseline."""
    height = top - base
    if abs(height) < 0.5:
        return
    y, h = (base, height) if height > 0 else (top, -height)
    radius = min(2.0, width / 2, h / 2)
    d.add(Rect(x, y, width, h, rx=radius, ry=radius, fillColor=fill, strokeColor=None))
    # Square off the end that sits on the baseline.
    foot = y if height > 0 else y + h - radius
    d.add(Rect(x, foot, width, radius, fillColor=fill, strokeColor=None))


def revenue_chart(v: Valuation, data: MarketData, width: float) -> Drawing | None:
    """Revenue and EBITDA by year: reported, then the model's forecast. A bank, insurer,
    utility or REIT has no EBITDA: its chart is net revenue and net income to common."""
    em = getattr(v, "equity", None)
    if v.family == "equity" and em is not None:
        forecast_labels = {y.label for y in em.years}
        income = {r["label"]: r.get("net_income") for r in em.reported}
        history = [h for h in data.history if h[0] not in forecast_labels][-4:]
        years = ([(label, rev, income.get(label), False) for label, rev, _ in history]
                 + [(y.label, y.revenue, y.net_income, True) for y in em.years])
        names = ("Net revenue", "Net income to common")
    else:
        forecast_labels = {r.label for r in v.rows}
        history = [h for h in data.history if h[0] not in forecast_labels][-4:]
        years = ([(label, rev, ebitda, False) for label, rev, ebitda in history]
                 + [(r.label, r.revenue, r.ebitda, True) for r in v.rows])
        names = ("Revenue", "EBITDA")
    years = [y for y in years if y[1] is not None or y[2] is not None]
    if len(years) < 2 or not any(y[3] for y in years):
        return None

    values = [x for _, rev, ebitda, _ in years for x in (rev, ebitda) if x is not None]
    scale, unit = (1e3, "bn") if max(abs(x) for x in values) >= 10_000 else (1.0, "mn")
    ticks = _nice_ticks(min(0.0, min(values) / scale), max(values) / scale * 1.08)
    lo, hi = ticks[0], ticks[-1]

    height = 190
    left, right, bottom, top = 34, 8, 20, 30
    plot_w, plot_h = width - left - right, height - bottom - top
    d = Drawing(width, height)
    y_of = lambda val: bottom + (val - lo) / (hi - lo) * plot_h

    tick = _tick_fmt(ticks)
    for t in ticks:
        y = y_of(t)
        d.add(Line(left, y, left + plot_w, y, strokeColor=AXIS if t == 0 else GRID,
                   strokeWidth=0.8 if t == 0 else 0.5))
        d.add(String(left - 4, y - 2.3, tick(t), fontName=FONT, fontSize=6.5,
                     fillColor=MUTED, textAnchor="end"))

    group = plot_w / len(years)
    bar = min(11.0, (group - 10) / 2)
    first_forecast = next(i for i, y in enumerate(years) if y[3])
    for i, (label, rev, ebitda, forecast) in enumerate(years):
        centre = left + group * (i + 0.5)
        for offset, amount, solid, light in ((-bar - 1, rev, BLUE, BLUE_LIGHT),
                                             (1, ebitda, ORANGE, ORANGE_LIGHT)):
            if amount is not None:
                _column(d, centre + offset, y_of(0), y_of(amount / scale), bar,
                        light if forecast else solid)
        d.add(String(centre, bottom - 11, label, fontName=FONT, fontSize=6.5,
                     fillColor=MUTED, textAnchor="middle"))

    # Label the last reported and the final forecast revenue: the path in two numbers.
    last_reported = first_forecast - 1
    for i in {last_reported, len(years) - 1} - {-1}:
        rev = years[i][1]
        if rev is not None:
            x = left + group * (i + 0.5) - bar / 2 - 1
            _label(d, x, y_of(rev / scale) + 3, _fmt(rev / scale), 6.5, INK_2, BOLD,
                   "middle")

    if first_forecast > 0:
        x = left + group * first_forecast
        d.add(Line(x, bottom, x, bottom + plot_h, strokeColor=AXIS, strokeWidth=0.6))
        d.add(String(x + 4, bottom + plot_h - 8, "Forecast", fontName=FONT, fontSize=6.5,
                     fillColor=INK_2))
        d.add(String(x - 4, bottom + plot_h - 8, "Reported", fontName=FONT, fontSize=6.5,
                     fillColor=INK_2, textAnchor="end"))

    # Legend, top left: swatch then name, in text ink.
    lx, ly = left, height - 12
    for name, fill in zip(names, (BLUE, ORANGE)):
        d.add(Rect(lx, ly, 8, 8, rx=1.5, ry=1.5, fillColor=fill, strokeColor=None))
        d.add(String(lx + 12, ly + 1.2, name, fontName=FONT, fontSize=7.5, fillColor=INK))
        lx += 16 + 7.5 * 0.55 * len(name) + 10
    d.add(String(width - right, ly + 1.2,
                 f"{v.reporting_currency} {unit}; lighter bars are forecasts",
                 fontName=FONT, fontSize=6.5, fillColor=MUTED, textAnchor="end"))
    return d


def roe_chart(v: Valuation, data: MarketData, width: float) -> Drawing | None:
    """For a bank or insurer: ROE by year, reported then forecast, then the terminal ROE,
    against the cost of equity and the ROE today's price assumes. The gap between the
    line and the cost of equity is what the excess-returns value is made of."""
    em = getattr(v, "equity", None)
    if v.family != "equity" or em is None or em.kind == "reit":
        return None
    forecast_labels = {y.label for y in em.years}
    points = ([(r["label"], r["roe"], False) for r in em.reported
               if r.get("roe") is not None and r["label"] not in forecast_labels][-4:]
              + [(y.label, y.roe, True) for y in em.years]
              + [("Terminal", em.terminal_roe, True)])
    if len(points) < 3:
        return None
    implied = getattr(v, "implied_margin", None)
    refs = [("Cost of equity", em.ke, ORANGE)]
    if implied is not None and math.isfinite(implied) and 0 < implied < 1:
        refs.append(("ROE the price assumes", implied, INK_2))
    values = [p for _, p, _ in points] + [r for _, r, _ in refs]
    ticks = _nice_ticks(min(0.0, min(values) * 100), max(values) * 100 * 1.12)
    lo, hi = ticks[0], ticks[-1]

    height = 170
    left, right, bottom, top = 34, 112, 20, 26
    plot_w, plot_h = width - left - right, height - bottom - top
    d = Drawing(width, height)
    y_of = lambda val: bottom + (val * 100 - lo) / (hi - lo) * plot_h
    tick = _tick_fmt(ticks)
    for t in ticks:
        y = bottom + (t - lo) / (hi - lo) * plot_h
        d.add(Line(left, y, left + plot_w, y, strokeColor=AXIS if t == 0 else GRID,
                   strokeWidth=0.8 if t == 0 else 0.5))
        d.add(String(left - 4, y - 2.3, tick(t) + "%", fontName=FONT, fontSize=6.5,
                     fillColor=MUTED, textAnchor="end"))

    group = plot_w / len(points)
    x_of = lambda i: left + group * (i + 0.5)
    for name, rate, colour in refs:
        y = y_of(rate)
        d.add(Line(left, y, left + plot_w, y, strokeColor=colour, strokeWidth=1,
                   strokeDashArray=[3, 2]))
        d.add(String(left + plot_w + 6, y - 2.3, f"{name} {rate:.1%}", fontName=FONT,
                     fontSize=6.5, fillColor=colour))
    for i in range(1, len(points)):
        d.add(Line(x_of(i - 1), y_of(points[i - 1][1]), x_of(i), y_of(points[i][1]),
                   strokeColor=BLUE, strokeWidth=1.4,
                   strokeDashArray=[2, 2] if points[i][0] == "Terminal" else None))
    for i, (label, roe, forecast) in enumerate(points):
        d.add(Circle(x_of(i), y_of(roe), 3.2, fillColor=SURFACE if forecast else BLUE,
                     strokeColor=BLUE, strokeWidth=1.2))
        _label(d, x_of(i), y_of(roe) + 6, f"{roe:.1%}", 6.5, INK_2, FONT, "middle")
        d.add(String(x_of(i), bottom - 11, label, fontName=FONT, fontSize=6.5,
                     fillColor=MUTED, textAnchor="middle"))
    first_forecast = next(i for i, p in enumerate(points) if p[2])
    if first_forecast > 0:
        x = left + group * first_forecast
        d.add(Line(x, bottom, x, bottom + plot_h, strokeColor=AXIS, strokeWidth=0.6))
        d.add(String(x + 4, bottom + plot_h - 8, "Forecast", fontName=FONT, fontSize=6.5,
                     fillColor=INK_2))
        d.add(String(x - 4, bottom + plot_h - 8, "Reported", fontName=FONT, fontSize=6.5,
                     fillColor=INK_2, textAnchor="end"))
    d.add(String(left, height - 12, "Return on equity: reported, forecast and terminal",
                 fontName=BOLD, fontSize=7.5, fillColor=INK))
    d.add(String(width, height - 12, "hollow points are forecasts", fontName=FONT,
                 fontSize=6.5, fillColor=MUTED, textAnchor="end"))
    return d


def _dcf_range(v: Valuation) -> tuple[float, float] | None:
    """The DCF across WACC +/-1pp and the terminal assumption +/-1 step of the grid."""
    if v.method == "perpetuity" and v.growth is not None:
        cells = [value_at(v, v.wacc + dw, growth=v.growth + dg)
                 for dw in (-0.01, 0.01) for dg in (-0.005, 0.005)]
    elif v.exit_multiple:
        cells = [value_at(v, v.wacc + dw, multiple=v.exit_multiple + dm)
                 for dw in (-0.01, 0.01) for dm in (-2, 2)]
    else:
        return None
    cells = [c for c in cells if c is not None and math.isfinite(c)]
    return (min(cells), max(cells)) if cells else None


def valuation_chart(v: Valuation, data: MarketData, width: float) -> Drawing | None:
    """Every value the pitch computes, against the current price."""
    other = "Exit-multiple cross-check" if v.method == "perpetuity" else "Perpetuity cross-check"
    rows: list[tuple[str, float, tuple[float, float] | None]] = [
        ("Excess returns (base case)" if v.family == "equity" else "DCF (base case)",
         v.per_share, _dcf_range(v))]
    if v.cross_check_per_share is not None:
        rows.append((other, v.cross_check_per_share, None))
    if v.downside_per_share is not None:
        rows.append(("Downside case", v.downside_per_share, None))
    if v.peers:
        if v.peers.value_ev_ebitda is not None:
            rows.append((f"Peers at {v.peers.median_ev_ebitda:.1f}x EV/EBITDA",
                         v.peers.value_ev_ebitda, None))
        if v.peers.value_pe is not None:
            rows.append((f"Peers at {v.peers.median_pe:.1f}x P/E", v.peers.value_pe, None))
    if data.analyst_target:
        count = f", {data.analyst_count} analysts" if data.analyst_count else ""
        rows.append((f"Street average{count}", data.analyst_target, None))
    if len(rows) < 2:
        return None

    points = [v.price] + [p for _, p, _ in rows] + [x for *_, r in rows if r for x in r]
    ticks = _nice_ticks(min(0.0, min(points)), max(points) * 1.06)
    lo, hi = ticks[0], ticks[-1]

    row_h, label_w, right, bottom, top = 21, 138, 78, 32, 22
    height = bottom + top + row_h * len(rows)
    plot_w = width - label_w - right
    d = Drawing(width, height)
    x_of = lambda val: label_w + (val - lo) / (hi - lo) * plot_w

    tick = _tick_fmt(ticks)
    for t in ticks:
        x = x_of(t)
        d.add(Line(x, bottom, x, height - top, strokeColor=GRID, strokeWidth=0.5))
        d.add(String(x, bottom - 11, tick(t), fontName=FONT, fontSize=6.5, fillColor=MUTED,
                     textAnchor="middle"))

    for i, (label, point, span) in enumerate(rows):
        y = height - top - row_h * (i + 0.5)
        d.add(String(label_w - 8, y - 2.5, label, fontName=BOLD if i == 0 else FONT,
                     fontSize=7.5, fillColor=INK if i == 0 else INK_2, textAnchor="end"))
        if span:
            d.add(Rect(x_of(span[0]), y - 4.5, x_of(span[1]) - x_of(span[0]), 9, rx=4.5,
                       ry=4.5, fillColor=BLUE_RANGE, strokeColor=None))
        fill = BLUE if i == 0 else INK_2
        d.add(Circle(x_of(point), y, 5.5 if i == 0 else 4.5, fillColor=SURFACE,
                     strokeColor=None))
        d.add(Circle(x_of(point), y, 4 if i == 0 else 3, fillColor=fill, strokeColor=None))
        text = f"{point:,.2f} ({pct((point / v.price - 1) * 100)})"
        end = max(x_of(point), x_of(span[1]) if span else 0)
        _label(d, end + 8, y - 2.5, text, 7, INK if i == 0 else INK_2,
               BOLD if i == 0 else FONT)

    x = x_of(v.price)
    d.add(Line(x, bottom, x, height - top + 6, strokeColor=INK, strokeWidth=0.8))
    d.add(String(x, height - top + 9, f"Current {data.currency} {v.price:,.2f}",
                 fontName=BOLD, fontSize=7, fillColor=INK, textAnchor="middle"))
    if rows[0][2]:
        low, high = rows[0][2]
        band = ("Band: cost of equity ±1pp and terminal growth ±0.5pp"
                if v.family == "equity" else
                "DCF band: WACC ±1pp and the terminal assumption one step either way")
        d.add(String(label_w, 3, f"{band}, {low:,.2f}-{high:,.2f}.",
                     fontName=FONT, fontSize=6.5, fillColor=MUTED))
    return d


def build(v: Valuation | None, data: MarketData, width: float) -> dict[str, Drawing]:
    """Every chart this pitch can draw, keyed by marker name. Never raises."""
    if v is None:
        return {}
    charts: dict[str, Drawing] = {}
    for name, make in (("revenue", revenue_chart), ("roe", roe_chart),
                       ("valuation", valuation_chart)):
        try:
            chart = make(v, data, width)
        except Exception as exc:     # the tables carry the numbers; a chart is extra
            log.warning("%s chart failed for %s: %s: %s", name, data.symbol,
                        type(exc).__name__, exc)
            chart = None
        if chart is not None:
            charts[name] = chart
    return charts
