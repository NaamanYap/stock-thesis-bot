"""Rules on the writer's `model` that the valuation does not need but the memo does: the
four quality assessments, the period a figure covers, and the log of method revisions.

The stock-thesis skill keeps these apart from the valuation. Quality is judged on the
business, not the price, so an excellent company can still be an expensive stock. A
reported quarter never stands for a forecast year (the skill's Comfort Systems SELL at
-89% came from one quarter read as the year). And a method choice the writer changes is
logged with its reason, written without reference to the rating, so the reader sees
every revision rather than only the final one.
"""

from __future__ import annotations

import re

import facts

QUALITY_AREAS = ("business_quality", "earnings_outlook", "balance_sheet",
                 "competitive_position")
QUALITY_RATINGS = ("strong", "adequate", "weak")
QUALITY_LABELS = {"business_quality": "Business quality",
                  "earnings_outlook": "Earnings outlook",
                  "balance_sheet": "Balance sheet",
                  "competitive_position": "Competitive position"}

# A base-to-first-year step in this band in a segment or an operating driver is a quarter
# taken for a year (about 4x) or a year taken for a quarter (about 0.25x). Below 3x is
# left alone: AVGO's AI revenue of 2026-10-01 nearly tripled in a forecast it argued.
PERIOD_BAND = (3.0, 5.5)

FIELD_PROMPT = """
- `quality` (required): four assessments of the business, kept apart from the valuation
  (an excellent company can be an expensive stock; do not judge the price here). Keys
  `business_quality`, `earnings_outlook`, `balance_sheet` and `competitive_position`, each
  `{"rating": "strong" | "adequate" | "weak", "summary": "one sentence with no typed
  numbers", "claims": ["T1", "M2"]}`, where `claims` are the outline point IDs it rests on.
  They are printed as their own table in Model Outputs.
- `method_revisions` (required when you change a method choice you made earlier: the
  terminal method, terminal growth, the exit multiple, beta, the WACC premium, the
  forecast length, the scenario weights, the valuation method or the conglomerate
  discount): a list of `{"choice", "from", "to",
  "reason"}`. Write the reason first: what was wrong with the earlier choice and what
  evidence in the notes shows it, without reference to the rating, the upside or the
  value it produces. If you cannot write that reason, leave the input alone. Every
  revision is printed in Model Outputs.
"""


def _words(text) -> str:
    return " ".join(str(text or "").split())


def quality_problems(model: dict, point_ids: set[str] | None = None) -> list[str]:
    """What is missing or wrong in `model.quality`. `point_ids` are the outline's IDs;
    with None the claims are not checked against it."""
    if not isinstance(model, dict):
        return []
    quality = model.get("quality")
    if not isinstance(quality, dict):
        return ["`model.quality` is missing: four assessments (" + ", ".join(
            f"`{a}`" for a in QUALITY_AREAS) + "), each with `rating` (strong, adequate or "
            "weak), a one-sentence `summary` with no typed numbers and the outline point "
            "`claims` it rests on. Judge the business here, not the price"]
    out = []
    for area in QUALITY_AREAS:
        item = quality.get(area)
        if not isinstance(item, dict):
            out.append(f"`model.quality.{area}` is missing")
            continue
        if str(item.get("rating") or "").strip().lower() not in QUALITY_RATINGS:
            out.append(f"`model.quality.{area}.rating` must be strong, adequate or weak")
        summary = _words(item.get("summary"))
        if len(summary.split()) < 6:
            out.append(f"`model.quality.{area}.summary` needs one sentence on why")
        else:
            typed = facts.typed(summary)
            if typed:
                out.append(f"`model.quality.{area}.summary` types "
                           + ", ".join(f.text for f, _ in typed)
                           + "; say it in words and let the claims carry the figures")
        claims = item.get("claims")
        if not isinstance(claims, list) or not claims:
            out.append(f"`model.quality.{area}.claims` needs the outline point IDs it rests on")
        elif point_ids is not None:
            unknown = [str(c) for c in claims if str(c) not in point_ids]
            if unknown:
                out.append(f"`model.quality.{area}.claims` names "
                           + ", ".join(f"`{c}`" for c in unknown)
                           + ", which are not outline point IDs")
    return out


def quality_table(model: dict) -> str:
    quality = model.get("quality") if isinstance(model, dict) else None
    if not isinstance(quality, dict):
        return ""
    cell = lambda x: _words(x).replace("|", "/") or "n.a."
    rows = []
    for area in QUALITY_AREAS:
        item = quality.get(area)
        if not isinstance(item, dict):
            continue
        claims = item.get("claims")
        claims = ", ".join(str(c) for c in claims) if isinstance(claims, list) else ""
        rows.append(f"| {QUALITY_LABELS[area]} | {cell(item.get('rating')).capitalize()} "
                    f"| {cell(item.get('summary'))} | {cell(claims)} |")
    if not rows:
        return ""
    return ("#### Quality, kept apart from the valuation\n\n"
            "How good the business is, judged without reference to the share price.\n\n"
            "| Area | Rating | Summary | Rests on |\n| --- | --- | --- | --- |\n"
            + "\n".join(rows))


def _numbers(values) -> list[float] | None:
    if isinstance(values, (int, float)) and not isinstance(values, bool):
        return [float(values)]
    if isinstance(values, list) and values and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in values):
        return [float(x) for x in values]
    return None


def _jump(label: str, start: float, first: float, year: str, what: str) -> str | None:
    if start <= 0 or first <= 0:
        return None
    ratio = first / start
    low, high = PERIOD_BAND
    if not (low <= ratio <= high or 1 / high <= ratio <= 1 / low):
        return None
    return (f"{what} `{label}` goes from {start:,.4g} in the base year to {first:,.4g} in "
            f"{year}, {ratio:.1f}x: a quarter's figure (or an annualised quarter) appears to "
            "stand for a full year, or a year for a quarter. Every value is the full fiscal "
            "year it is labelled with; a reported quarter never describes a forecast year")


def period_problems(model: dict) -> list[str]:
    """Segments and operating drivers whose first forecast year is several times the base
    year or a fraction of it: one of the two is a different period."""
    if not isinstance(model, dict):
        return []
    years = model.get("years") if isinstance(model.get("years"), list) else []
    first_year = str(years[0]) if years else "the first forecast year"
    out = []
    segments = model.get("segments") if isinstance(model.get("segments"), dict) else {}
    base = model.get("segment_base") if isinstance(model.get("segment_base"), dict) else {}
    for name, values in segments.items():
        path, start = _numbers(values), _numbers(base.get(name))
        if path and start:
            problem = _jump(str(name), start[0], path[0], first_year, "segment")
            if problem:
                out.append(problem)
    drivers = model.get("drivers") if isinstance(model.get("drivers"), dict) else {}
    for name, spec in drivers.items():
        if not isinstance(spec, dict) or "%" in str(spec.get("unit") or ""):
            continue
        path, start = _numbers(spec.get("values")), _numbers(spec.get("base"))
        if path and start:
            problem = _jump(str(name), start[0], path[0], first_year, "driver")
            if problem:
                out.append(problem)
    return out


# --------------------------------------------------------------------------- revisions

def _terminal(model: dict) -> dict:
    t = model.get("terminal")
    return t if isinstance(t, dict) else {}


def _weights(model: dict):
    p = model.get("scenario_probabilities")
    if not isinstance(p, dict):
        return None
    try:
        return tuple(round(float(p.get(k)), 3) for k in ("bull", "base", "bear"))
    except (TypeError, ValueError):
        return None


# The method choices the skill's `method.revisions` covers, as (name, reader).
CHOICES = (
    ("terminal method", lambda m: _terminal(m).get("method")),
    ("terminal growth", lambda m: _terminal(m).get("growth")),
    ("exit multiple", lambda m: _terminal(m).get("exit_ev_ebitda")
     or _terminal(m).get("exit_ev_ebit")),
    ("beta", lambda m: m.get("beta")),
    ("wacc premium", lambda m: m.get("wacc_premium")),
    ("forecast length", lambda m: len(m["years"]) if isinstance(m.get("years"), list)
     else None),
    ("scenario weights", _weights),
    ("valuation method", lambda m: str(m.get("valuation_method") or "dcf").lower()),
    ("conglomerate discount", lambda m: (m.get("conglomerate_discount") or {}).get("rate")
     if isinstance(m.get("conglomerate_discount"), dict) else None),
)

RATING_TALK = re.compile(r"\b(?:BUY|SELL|HOLD|rating|upside|downside to|intrinsic value|"
                         r"price target|fair value|value per share)\b", re.I)


def _shown(value) -> str:
    if isinstance(value, tuple):
        return "/".join(f"{x:g}" for x in value)
    if isinstance(value, float):
        return f"{value:g}"
    return "none" if value is None else str(value)


def method_changes(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """(choice, from, to) for each method choice that differs between two models."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    out = []
    for name, read in CHOICES:
        a, b = read(old), read(new)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) < 1e-9:
                continue
        elif a == b:
            continue
        out.append((name, _shown(a), _shown(b)))
    return out


def _entries(model: dict) -> list[dict]:
    items = model.get("method_revisions") if isinstance(model, dict) else None
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _normal(text) -> str:
    return re.sub(r"[^a-z]+", " ", str(text or "").lower()).strip()


def revision_problems(old: dict, new: dict) -> list[str]:
    """A method choice changed since `old` without a logged reason, or a logged reason
    that argues from the result."""
    out = []
    entries = _entries(new)
    for name, a, b in method_changes(old, new):
        logged = [e for e in entries if name in _normal(e.get("choice"))]
        if not logged:
            out.append(f"the {name} changed from {a} to {b} with no `method_revisions` entry "
                       f"(`choice`: \"{name}\"): say what was wrong with {a} and what evidence "
                       "shows it, or keep the earlier choice")
        elif all(len(_words(e.get("reason")).split()) < 8 for e in logged):
            out.append(f"the `method_revisions` entry for the {name} needs a `reason`: what "
                       "was wrong and the evidence that shows it")
    for e in entries:
        if RATING_TALK.search(str(e.get("reason") or "")):
            out.append(f"the `method_revisions` reason for the {_words(e.get('choice'))} "
                       "argues from the result; a revision is justified by the method being "
                       "wrong, never by the rating or value it produces")
    return out


def revisions_table(model: dict) -> str:
    entries = _entries(model)
    if not entries:
        return ""
    cell = lambda x: _words(x).replace("|", "/") or "n.a."
    rows = "\n".join(f"| {cell(e.get('choice'))} | {cell(e.get('from'))} | {cell(e.get('to'))} "
                     f"| {cell(e.get('reason'))} |" for e in entries)
    return ("#### Method revisions\n\nEach change to a method choice made while the model "
            "was built, with the reason given before the rerun.\n\n"
            "| Choice | From | To | Reason |\n| --- | --- | --- | --- |\n" + rows)
