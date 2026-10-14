"""Conglomerates: margins by segment, a sum of the parts tied to the forecast, a
conglomerate discount, and the sum of the parts as the primary method when argued.

Until 2026-10-12 a conglomerate had one consolidated margin, so the forecast could not
show margin moving with the mix, and its `parts` were typed amounts nothing checked
against the forecast, shown beside a DCF the rating always followed.
"""

from __future__ import annotations

import math

# Each part's amount must agree with the forecast this closely.
PARTS_TIE_TOLERANCE = 0.10
# A typed consolidated margin this far from the one the segments build is ignored, said.
MARGIN_GAP_PP = 0.5
MAX_DISCOUNT = 0.35
SOTP = "sum_of_parts"

FIELD_PROMPT = """
- `segment_margins` (recommended when segments earn very different margins, required for a
  conglomerate): each segment's operating (EBIT) margin per forecast year, as fractions,
  `{"Aerospace": [...], "Industrial": [...]}`, covering every segment, with
  `corporate_costs.amount` the head office's yearly cost (reporting mn, or a list per
  year). The code then builds the consolidated margin itself, so mix shifts move it; leave
  `ebit_margin` out. A part valued on EBIT is checked against its segment's forecast EBIT,
  and parts on EBITDA against the company's EBITDA, within 10%.
- `conglomerate_discount` (required with `parts`): `{"rate": 0.10, "reason": "..."}`, the
  discount from the sum of the parts to what the market pays for the group, 0 to 0.35,
  argued from evidence: the group's own history against its parts, peers that broke up,
  capital allocation across the segments. A rate of 0 needs its reason too.
- `valuation_method` (optional): `"sum_of_parts"` when the parts, not one DCF, are the right
  way to value this company, with `valuation_method_reason` saying why from the evidence.
  The rating then follows the sum of the parts after the discount, and the DCF becomes the
  cross-check. It is a method choice: a change to it is logged in `method_revisions`.
"""


class MarginError(ValueError):
    """Segment margins that cannot be applied."""


def _path(x, n: int, what: str) -> list[float]:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return [float(x)] * n
    if (not isinstance(x, list) or len(x) != n
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in x)):
        raise MarginError(f"{what} needs {n} yearly values")
    return [float(v) for v in x]


def apply_margins(model: dict) -> tuple[dict, dict | None]:
    """The model with `ebit_margin` built from `segment_margins` and the segments'
    revenue, less corporate costs; and what was built (None without segment margins)."""
    raw = model.get("segment_margins")
    if raw is None:
        return model, None
    years = model.get("years") if isinstance(model.get("years"), list) else []
    n = len(years)
    segments = model.get("segments")
    if not isinstance(raw, dict) or not raw:
        raise MarginError("`model.segment_margins` must map segment names to yearly margins")
    if not isinstance(segments, dict) or not segments:
        raise MarginError("`model.segment_margins` needs the revenue by segment it applies "
                          "to (`segments` or `revenue_build`)")
    missing = [s for s in segments if s not in raw]
    stray = [s for s in raw if s not in segments]
    if missing or stray:
        raise MarginError("`model.segment_margins` must cover exactly the segments: "
                          + "; ".join(x for x in (
                              "missing " + ", ".join(missing) if missing else "",
                              "not a segment: " + ", ".join(stray) if stray else "") if x))
    margins = {s: _path(raw[s], n, f"segment margin `{s}`") for s in segments}
    if any(abs(m) > 1 for path in margins.values() for m in path):
        raise MarginError("segment margins are fractions (0.15 is 15%)")
    corp = model.get("corporate_costs") if isinstance(model.get("corporate_costs"), dict) else {}
    corporate = ([abs(x) for x in _path(corp["amount"], n, "corporate_costs.amount")]
                 if corp.get("amount") is not None else [0.0] * n)
    revenue = [sum(float(segments[s][i]) for s in segments) for i in range(n)]
    ebit = {s: [float(segments[s][i]) * margins[s][i] for i in range(n)] for s in segments}
    total = [sum(ebit[s][i] for s in segments) - corporate[i] for i in range(n)]
    built = [t / r if r else 0.0 for t, r in zip(total, revenue)]
    warnings = []
    typed = model.get("ebit_margin")
    if typed is not None:
        try:
            typed_path = _path(typed, n, "ebit_margin")
        except MarginError:
            typed_path = None
        if typed_path and any(abs(a - b) * 100 > MARGIN_GAP_PP
                              for a, b in zip(typed_path, built)):
            warnings.append("`model.ebit_margin` differs from the margin the segment margins "
                            "build; the segments' is used, so leave `ebit_margin` out")
    model = dict(model)
    model["ebit_margin"] = built
    return model, {"margins": margins, "ebit": ebit, "corporate": corporate,
                   "built": built, "warnings": warnings}


def discount(model: dict) -> float:
    raw = model.get("conglomerate_discount") if isinstance(model, dict) else None
    rate = raw.get("rate") if isinstance(raw, dict) else raw
    if isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate):
        return min(max(float(rate), 0.0), MAX_DISCOUNT)
    return 0.0


def is_primary(model: dict) -> bool:
    return isinstance(model, dict) and str(model.get("valuation_method") or "").strip().lower() \
        in (SOTP, "sotp", "sum of the parts")


def problems(model: dict, v) -> list[str]:
    """What a conglomerate's model still owes: the discount argued, the primary method
    argued, and parts that agree with the forecast."""
    if not isinstance(model, dict) or v is None:
        return []
    out = []
    parts = model.get("parts") if isinstance(model.get("parts"), dict) else {}
    if parts:
        raw = model.get("conglomerate_discount")
        rate = raw.get("rate") if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or not isinstance(rate, (int, float)) \
                or len(str(raw.get("reason") or "").split()) < 8:
            out.append("`model.conglomerate_discount` is missing: with `parts`, give `rate` "
                       "(0 to 0.35) and a `reason` from the evidence for the discount from "
                       "the sum of the parts to the group, or for none")
        elif not 0 <= rate <= MAX_DISCOUNT:
            out.append(f"`model.conglomerate_discount.rate` must be 0 to {MAX_DISCOUNT}")
    if is_primary(model):
        if not parts or not getattr(v, "parts", None):
            out.append("`valuation_method` is sum_of_parts but the model gives no `parts` "
                       "to value")
        if len(str(model.get("valuation_method_reason") or "").split()) < 8:
            out.append("`valuation_method_reason` is required with sum_of_parts: why the "
                       "parts, not one DCF, value this company, from the evidence")
    out += tie_problems(model, v)
    return out


def tie_problems(model: dict, v) -> list[str]:
    """Parts whose amounts disagree with the forecast they claim to value."""
    built = getattr(v, "segment_margins", None)
    vparts = getattr(v, "parts", None)
    if not vparts or not getattr(v, "rows", None):
        return []
    labels = [r.label for r in v.rows]
    out = []
    ebitda_parts: dict[str, float] = {}
    for r in vparts["rows"]:
        if r["metric"] == "cost":
            continue
        t = labels.index(r["year"]) if r["year"] in labels else 0
        metric = r["metric"].strip().upper()
        if metric == "EBIT" and built and r["name"] in built["ebit"]:
            want = built["ebit"][r["name"]][t]
            if want and abs(r["amount"] / want - 1) > PARTS_TIE_TOLERANCE:
                out.append(f"part `{r['name']}` is valued on {r['amount']:,.0f} of {r['year']} "
                           f"EBIT, but the forecast gives that segment {want:,.0f}; value the "
                           "part on the forecast's figure or change the forecast")
        elif metric == "EBITDA":
            ebitda_parts[r["year"]] = ebitda_parts.get(r["year"], 0.0) + r["amount"]
    corp = model.get("corporate_costs") if isinstance(model.get("corporate_costs"), dict) else {}
    head = corp.get("amount")
    head = abs(head if isinstance(head, (int, float)) else head[0]) \
        if isinstance(head, (int, float, list)) and head else 0.0
    other = sum(1 for r in vparts["rows"] if r["metric"] not in ("cost",)
                and r["metric"].strip().upper() != "EBITDA")
    if len(ebitda_parts) == 1 and not other:
        year, total = next(iter(ebitda_parts.items()))
        t = labels.index(year) if year in labels else 0
        want = v.rows[t].ebitda
        got = total - head
        if want and want > 0 and abs(got / want - 1) > PARTS_TIE_TOLERANCE:
            out.append(f"the parts' {year} EBITDA less corporate costs comes to {got:,.0f}, "
                       f"but the forecast's EBITDA for {year} is {want:,.0f}; the parts must "
                       "add up to the company the DCF forecasts")
    return out


def margins_table(v) -> str:
    built = getattr(v, "segment_margins", None)
    if not built or not v.rows:
        return ""
    picks = sorted({0, min(2, len(v.rows) - 1), len(v.rows) - 1})
    head = ["Operating margin by segment"] + [v.rows[i].label for i in picks]
    rows = [[s] + [f"{built['margins'][s][i]:.1%}" for i in picks] for s in built["margins"]]
    if any(built["corporate"]):
        rows.append(["Corporate costs (% of sales)"]
                    + [f"{-built['corporate'][i] / v.rows[i].revenue:.1%}" if v.rows[i].revenue
                       else "n.a." for i in picks])
    rows.append(["**Group operating margin**"] + [f"**{built['built'][i]:.1%}**" for i in picks])
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return ("#### Margins by segment\n\nThe group margin is built in code from each segment's "
            "margin and sales, less corporate costs.\n\n" + "\n".join(lines))
