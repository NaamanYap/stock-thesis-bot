"""Revenue built in code from volume times price, line by line, as the stock-thesis skill
builds it.

Until 2026-10-12 the writer typed each segment's revenue and up to four drivers were only
a cross-check, a 10% mismatch being a warning. Now the writer gives each line's base
volume and price and their growth, and the code multiplies them out. A line's volume can
come from a market chain (market size x penetration x share), each link with its own
path, label and definition. The lines must rebuild the base year's revenue within 2%, or
the model is not valued: a build that does not reproduce the reported year is a stop.

The built lines become `model.segments` and `model.segment_base`, and each line's volume
a driver of its segment, so the rest of the valuation and the Model Outputs tables read
the build the way they read typed segments.
"""

from __future__ import annotations

import math
import re

# The lines must rebuild the base year's revenue this closely.
REBUILD_TOLERANCE = 0.02
# The rescales tried on one line's price when the build misses the base year.
UNIT_FACTORS = (1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)
# A volume line building less than this share of the base year gets named in the stop.
TINY_LINE = 0.001
# A growth rate above this is a percentage given where a fraction is wanted.
GROWTH_AS_PERCENT = 1.5

FIELD_PROMPT = """
- `revenue_build` (required unless the company reports no usable volume or price measure):
  revenue built in code from its drivers, one entry per line of business. The code
  multiplies volume by price per line and year, sums the lines into `segments`, and derives
  revenue growth itself, so leave `segments`, `segment_base` and `revenue` out when you
  give it. Each line is
  `{"line": "Premium", "segment": "Premium", "volume_unit": "mn subscribers",
  "base_volume": 290, "base_price": 52.9, "volume_growth": [...], "price_growth": [...],
  "basis": "sourced" | "analyst", "definition": "what the volume counts, what the price
  is, where both come from and how you project them"}`: `base_price` is revenue per unit
  in the reporting currency (full units, not millions), growth rates are fractions per
  forecast year (0.05 is 5%), and every value is a full fiscal year, never a quarter. A
  line with no volume measure is `{"line": "Other", "base_revenue": 1200, "growth": [...]}`
  (reporting mn). **The base volumes times base prices, plus any direct lines, must rebuild
  `base_revenue` within 2%**: otherwise the model is not valued.
  Where volume comes from a market, give `market_chain` on the line instead of
  `volume_growth`: a list of links whose product is the volume, each `{"name": "Smartphone
  users", "unit": "mn", "base": 4800, "values": [...], "label": "sourced" | "analyst",
  "definition": "how the link is measured, why this measure, and what you assumed beyond
  the last sourced year"}`, with penetration and share written as fractions (0.62). The
  product of the bases must match `base_volume` within 2%. The chain's path becomes the
  line's volume growth. If the company reports no usable volume or price measure, leave
  `revenue_build` out and give `revenue_build_reason`: one sentence saying so.
"""


class BuildError(ValueError):
    """The build cannot be computed, or does not rebuild the base year."""


def _scale(unit: str) -> float:
    u = str(unit or "").lower()
    if re.search(r"\bbn\b|billion", u):
        return 1e9
    if re.search(r"\bmn\b|\bm\b|million", u):
        return 1e6
    if re.search(r"thousand|'000|\b000s?\b|\bk\b", u):
        return 1e3
    return 1.0


def _num(x, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise BuildError(f"{what} must be a number")
    return float(x)


def _path(x, n: int, what: str) -> list[float]:
    """One value per forecast year; a single number applies to every year."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return [float(x)] * n
    if not isinstance(x, list) or len(x) != n:
        raise BuildError(f"{what} needs {n} yearly values")
    return [_num(v, what) for v in x]


def _compound(start: float, growth: list[float]) -> list[float]:
    out, level = [], start
    for g in growth:
        level *= 1 + g
        out.append(level)
    return out


def _chain(line: str, links, base_volume: float, n: int) -> tuple[list[float], list[dict]]:
    """The line's volume path from its market chain, and the links as built."""
    if not isinstance(links, list) or not links:
        raise BuildError(f"`{line}` market_chain must be a list of links")
    built, base_product, product = [], 1.0, [1.0] * n
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            raise BuildError(f"`{line}` market_chain link {i + 1} must be an object")
        name = str(link.get("name") or f"link {i + 1}")
        base = _num(link.get("base"), f"`{line}` market_chain `{name}` base")
        values = _path(link.get("values"), n, f"`{line}` market_chain `{name}` values")
        if base <= 0 or any(v <= 0 for v in values):
            raise BuildError(f"`{line}` market_chain `{name}` must be positive")
        base_product *= base
        product = [p * v for p, v in zip(product, values)]
        built.append({"name": name, "unit": str(link.get("unit") or ""), "base": base,
                      "values": values, "label": str(link.get("label") or ""),
                      "definition": " ".join(str(link.get("definition") or "").split())})
    if abs(base_product / base_volume - 1) > REBUILD_TOLERANCE:
        raise BuildError(
            f"`{line}`: the market chain's base links multiply to {base_product:,.4g}, not "
            f"the line's base_volume of {base_volume:,.4g}; the chain must produce the "
            "volume it builds (write penetration and share as fractions)")
    return [base_volume * p / base_product for p in product], built


def build(model: dict, base_revenue: float, n: int, currency: str = "") -> dict | None:
    """The build as {"segments", "segment_base", "drivers", "chains", "lines",
    "warnings"}, or None when the model gives no `revenue_build`. Raises BuildError when it
    cannot be computed or does not rebuild the base year."""
    lines = model.get("revenue_build")
    if lines is None:
        return None
    if not isinstance(lines, list) or not lines:
        raise BuildError("`model.revenue_build` must be a list of revenue lines")
    segments: dict[str, list[float]] = {}
    segment_base: dict[str, float] = {}
    drivers: dict[str, dict] = {}
    chains: dict[str, list[dict]] = {}
    built_lines, warnings = [], []
    for i, spec in enumerate(lines):
        if not isinstance(spec, dict):
            raise BuildError(f"revenue_build line {i + 1} must be an object")
        name = str(spec.get("line") or f"line {i + 1}")
        seg = str(spec.get("segment") or name)
        if spec.get("base_volume") is not None:
            unit = str(spec.get("volume_unit") or "")
            vol0 = _num(spec.get("base_volume"), f"`{name}` base_volume")
            price0 = _num(spec.get("base_price"), f"`{name}` base_price")
            if vol0 <= 0 or price0 <= 0:
                raise BuildError(f"`{name}` needs a positive base_volume and base_price")
            if spec.get("market_chain") is not None:
                volume, links = _chain(name, spec["market_chain"], vol0, n)
                chains[seg] = links
                if spec.get("volume_growth") is not None:
                    warnings.append(f"revenue line `{name}` gives both a market_chain and "
                                    "volume_growth; the chain sets the volume")
                vgrowth = [b / a - 1 for a, b in zip([vol0] + volume[:-1], volume)]
            else:
                vgrowth = _path(spec.get("volume_growth"), n, f"`{name}` volume_growth")
                volume = _compound(vol0, vgrowth)
            pgrowth = _path(spec.get("price_growth", 0.0), n, f"`{name}` price_growth")
            price = _compound(price0, pgrowth)
            scale = _scale(unit) / 1e6
            base = vol0 * price0 * scale
            revenue = [v * p * scale for v, p in zip(volume, price)]
            for label, path in (("volume_growth", vgrowth), ("price_growth", pgrowth)):
                if any(abs(g) > GROWTH_AS_PERCENT for g in path):
                    warnings.append(f"revenue line `{name}` has a {label} above "
                                    f"{GROWTH_AS_PERCENT:.0%} in a year; growth rates are "
                                    "fractions (0.05 is 5%)")
                    break
            drivers.setdefault(f"{name} volume", {
                "unit": unit, "segment": seg, "base": vol0, "values": volume,
                "basis": str(spec.get("basis") or "analyst"),
                "definition": " ".join(str(spec.get("definition") or "").split())
                or f"{name} volume in {unit or 'units'}, built in code from its base and "
                   "growth"})
            built_lines.append({"line": name, "segment": seg, "unit": unit, "base_volume": vol0,
                                "volume": volume, "base_price": price0, "price": price,
                                "base_revenue": base, "revenue": revenue})
        elif spec.get("base_revenue") is not None:
            base = _num(spec.get("base_revenue"), f"`{name}` base_revenue")
            growth = _path(spec.get("growth"), n, f"`{name}` growth")
            revenue = _compound(base, growth)
            built_lines.append({"line": name, "segment": seg, "base_revenue": base,
                                "revenue": revenue})
        else:
            raise BuildError(f"revenue line `{name}` needs base_volume and base_price, or "
                             "base_revenue for a line with no volume measure")
        segment_base[seg] = segment_base.get(seg, 0.0) + base
        segments[seg] = [a + b for a, b in zip(segments.get(seg, [0.0] * n), revenue)]
    total = sum(segment_base.values())
    fixed = _unit_slip(built_lines, total, base_revenue)
    if fixed is not None:
        # TSLA of 2026-10-13: energy priced at $641.35 "per GWh" meaning $641.35mn, the
        # build 13.6% short, and all three writes lost the valuation to it.
        line, factor = fixed
        seg = line["segment"]
        delta = line["base_revenue"] * (factor - 1)
        segment_base[seg] += delta
        segments[seg] = [a + r * (factor - 1) for a, r in zip(segments[seg], line["revenue"])]
        total += delta
        line["base_price"] *= factor
        line["price"] = [p * factor for p in line["price"]]
        line["base_revenue"] *= factor
        line["revenue"] = [r * factor for r in line["revenue"]]
        size = f"{factor:,.0f}" if factor > 1 else f"1/{1 / factor:,.0f}"
        warnings.append(
            f"revenue line `{line['line']}`: base_price was rescaled by {size} to rebuild "
            f"the base year and was read as {line['base_price']:,.0f} per "
            f"{line['unit'] or 'unit'}; give base_price as revenue per single unit in full "
            "currency units (not thousands or millions), with any scale in volume_unit "
            "(k, mn, bn)")
    if not base_revenue or abs(total / base_revenue - 1) > REBUILD_TOLERANCE:
        shown = "; ".join(f"{x['line']} {x['base_revenue']:,.0f}" for x in built_lines)
        # A line that builds next to nothing is nearly always a price in the wrong unit.
        tiny = [x["line"] for x in built_lines
                if "base_volume" in x and x["base_revenue"] < TINY_LINE * base_revenue]
        hint = (f" `{'`, `'.join(tiny)}` builds next to nothing: base_price is revenue per "
                "single unit in full currency units, not thousands or millions."
                if tiny and base_revenue else "")
        raise BuildError(
            f"the revenue build gives {total:,.0f} for the base year against base_revenue of "
            f"{base_revenue:,.0f} ({currency} mn), {total / base_revenue - 1:+.1%} "
            f"({shown}). Base volume x base price per line must rebuild the reported year "
            "within 2%: check each line's volume, price and units against the filing, and "
            f"add a direct line for revenue no volume measure covers.{hint}")
    return {"segments": segments, "segment_base": segment_base, "drivers": drivers,
            "chains": chains, "lines": built_lines, "warnings": warnings}


def _unit_slip(lines: list[dict], total: float, base_revenue: float):
    """The one volume line, and the power of 1,000, that would rebuild the base year when
    the build misses it; None when it already rebuilds, or when no single rescale (or more
    than one) closes the gap. A price per unit given in thousands or millions is the usual
    slip, and it is unambiguous when exactly one line's rescale lands within tolerance."""
    if not base_revenue or abs(total / base_revenue - 1) <= REBUILD_TOLERANCE:
        return None
    hits = [(line, factor) for line in lines if "base_volume" in line
            for factor in UNIT_FACTORS
            if abs((total + line["base_revenue"] * (factor - 1)) / base_revenue - 1)
            <= REBUILD_TOLERANCE]
    return hits[0] if len(hits) == 1 else None


def apply(model: dict, base_revenue: float, n: int, currency: str = "") -> tuple[dict, dict | None]:
    """The model with its revenue built from `revenue_build`, and the build (None when the
    model has none). The model is copied, never changed in place."""
    out = build(model, base_revenue, n, currency)
    if out is None:
        return model, None
    model = dict(model)
    model.pop("revenue", None)
    model["segments"], model["segment_base"] = out["segments"], out["segment_base"]
    drivers = dict(model.get("drivers") or {}) if isinstance(model.get("drivers"), dict) else {}
    # The build's volumes are the drivers of their segments; any the writer typed for the
    # same segment give way.
    built_segments = {d["segment"] for d in out["drivers"].values()}
    drivers = {k: v for k, v in drivers.items()
               if not (isinstance(v, dict) and v.get("segment") in built_segments)}
    drivers.update(out["drivers"])
    model["drivers"] = drivers
    return model, out


def missing_problems(model: dict) -> list[str]:
    """The skill prefers a build; a model without one says why the company has no usable
    volume or price measure."""
    if not isinstance(model, dict) or model.get("revenue_build") is not None:
        return []
    if len(str(model.get("revenue_build_reason") or "").split()) >= 8:
        return []
    return ["`model.revenue_build` is missing: build revenue as volume x price per line of "
            "business (base volume, base price and their growth, or a market chain), which "
            "the code multiplies out and checks against the base year; if the company "
            "reports no usable volume or price measure, give `revenue_build_reason` saying "
            "so in one sentence"]


def chain_table(chains: dict[str, list[dict]], labels: list[str]) -> str:
    """The market chains, link by link: base, first, middle and final forecast year."""
    if not chains:
        return ""
    picks = sorted({0, min(2, len(labels) - 1), len(labels) - 1})
    head = ["Market chain", "Unit", "Base"] + [labels[i] for i in picks] + ["Label"]
    rows = []
    for seg, links in chains.items():
        for link in links:
            fmt = (lambda x: f"{x:.1%}") if link["base"] <= 1 and all(
                v <= 1 for v in link["values"]) else (lambda x: f"{x:,.4g}")
            rows.append([f"{seg}: {link['name']}", link["unit"] or "", fmt(link["base"])]
                        + [fmt(link["values"][i]) for i in picks] + [link["label"] or "n.a."])
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    lines += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    notes = [f"*{seg}: {link['name']}.* {link['definition']}"
             for seg, links in chains.items() for link in links if link["definition"]]
    return ("#### Market chain\n\nEach line's volume is the product of its links, computed in "
            "code.\n\n" + "\n".join(lines) + ("\n\n" + "\n\n".join(notes) if notes else ""))
