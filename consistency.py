"""Check that the prose agrees with the computed model.

The tables and the cover come from valuation.py, so they cannot disagree with each
other. The prose still can: a model that ignores the placeholders and types its own
figures will quote an intrinsic price, an upside or a forecast-year cash flow that the
model does not produce. The NVDA pitch of 2026-09-06 gave FY2032 free cash flow as
$260bn in the text, $313bn in one table and $338bn in another.

This reads the finished body — placeholders already filled in — and reports the stated
intrinsic price, upside, rating, comparisons and today's multiple where the computed
valuation contradicts them. A forecast year's figures are provenance.py's: it pins each
to the metric it is a figure of, which this module's phrase patterns never could (NFLX's
"$5-6 billion by 2030" of advertising was read here as its 2030 revenue).
"""

from __future__ import annotations

import re

from provenance import SENTENCE as SENTENCE_SPLIT
from valuation import Valuation, pct

# Relative tolerance on an amount, before allowing for how coarsely it was written.
AMOUNT_TOLERANCE = 0.02
UPSIDE_TOLERANCE_PP = 2.0
MARGIN_TOLERANCE_PP = 1.0
MULTIPLE_TOLERANCE = 0.10

CURRENCY = r"(?:US\$|[A-Z]{2,3}\$|\$|€|£|¥|(?:USD|EUR|GBP|JPY|TWD|HKD|CNY|SGD)\s?)"
NUMBER = r"(\d[\d,]*(?:\.\d+)?)"
SCALE = {"tn": 1e6, "trillion": 1e6, "bn": 1e3, "billion": 1e3, "b": 1e3,
         "mn": 1.0, "million": 1.0, "m": 1.0}

INTRINSIC_RE = re.compile(
    r"(?:intrinsic|implied)\s+(?:share\s+)?(?:price|value)(?:\s+per\s+share)?"
    r"(?:\s+(?:of|is|at|to))?\s*[:=]?\s*(?:of\s+)?" + CURRENCY + r"?\s*" + NUMBER, re.I)
UPSIDE_AFTER_RE = re.compile(r"([+\-−]?\d+(?:\.\d+)?)%\s+(upside|downside)", re.I)
UPSIDE_BEFORE_RE = re.compile(
    r"\b(upside|downside)\s+(?:of\s+|is\s+|at\s+)?(?:c\.\s*)?([+\-−]?\d+(?:\.\d+)?)%", re.I)
RATING_RES = [
    re.compile(r"Recommendation:\s*\**\s*(BUY|HOLD|SELL)\b"),
    re.compile(r"\b(?:rate|rating|recommend|recommendation|initiate)\b[^.\n]{0,40}?"
               r"\b(BUY|HOLD|SELL)\b"),
]

# What the stock trades at today. NEE's cover of 2026-09-26 called it "fully valued at 19x
# EV/EBITDA" when the market paid 15.0x trailing, the multiple its own exit assumed.
TRADING_MULTIPLE_RE = re.compile(
    r"\b(?:trades?|trading|valued|priced)\s+(?:at\s+)?"
    r"(?:c\.\s*|~|about\s+|roughly\s+|around\s+)?(\d+(?:\.\d+)?)x\s+"
    r"(?:(?:trailing|TTM|LTM|current)\s+)?(?:EV\s*/\s*)?EBITDA\b", re.I)
# Sentences about another company, the exit year or a hypothetical quote multiples that
# are rightly not today's.
NOT_TODAY = re.compile(r"\b(?:exit|terminal|DCF|implie[sd]|imply|would|if|forward|"
                       r"FY\s?'?\d{2,4}|20[3-9]\d)\b", re.I)
# Sentences about the market, the Street or the company's own guidance quote figures
# that are rightly not the pitch's forecast.
SKIP_WORDS = re.compile(r"consensus|guidance|guided|street|\bTAM\b|market size|addressable|"
                        r"industry|analysts?\b|peers?\b|competitor", re.I)
# A signed figure and a direction word that disagree: the NFLX pitch of 2026-09-23 put
# its value "-9% above the consensus target" after {{vs_consensus}} filled in a minus.
DIRECTION_RE = re.compile(
    r"([+\-−]\d+(?:\.\d+)?)%\s+(above|below|higher than|lower than|premium|discount)\b", re.I)
UP_WORDS = {"above", "higher than", "premium"}
FAIR_PRICE_RE = re.compile(r"\b(?:fairly\s+(?:priced|valued)|at\s+fair\s+value)\b", re.I)
CHEAP_PRICE_RE = re.compile(r"\b(?:undervalued|underpriced|cheap(?:ly)?\s+(?:valued|priced)?)\b", re.I)
EXPENSIVE_PRICE_RE = re.compile(r"\b(?:overvalued|overpriced|expensive(?:ly)?\s+(?:valued|priced)?)\b", re.I)
# "I would change the recommendation to SELL if the merger terminates" names the rating
# the pitch would move to, not the one it gives: the NEE pitch of 2026-09-24 shipped with
# that flagged as a contradiction on its cover.
CONDITIONAL = re.compile(r"\b(?:would|if|unless|should|change|downgrade|upgrade|revise|"
                         r"move (?:to|it)|trigger)", re.I)
# The DCF set against the peers or the Street, with the direction in words. The AVGO pitch
# of 2026-09-24 explained why "the DCF exceeds the peer multiple" when it sat 53% below it:
# the prose is written before the code computes the value.
COMPARE_RE = re.compile(
    r"\b(?:DCF|intrinsic value|my value|our value|my estimate)\b[^.;:]{0,40}?"
    r"\b(exceeds?|above|higher than|below|lower than|short of)\b[^.;]{0,40}?"
    r"\b(peer|consensus|street|sell-side)", re.I)
COMPARE_UP = {"exceed", "exceeds", "above", "higher than"}


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def _slack(written: str, scale: float) -> float:
    """Half a unit in the last digit written: '$0.3tn' can mean anything from 0.25 to 0.35."""
    decimals = len(written.split(".")[1]) if "." in written else 0
    return 0.5 * 10 ** -decimals * scale


def _close(stated: float, written: str, scale: float, actual: float) -> bool:
    allowed = max(AMOUNT_TOLERANCE * abs(actual), _slack(written, scale))
    return abs(stated - actual) <= allowed


def _signed(text: str) -> float:
    return _to_float(text.replace("−", "-").lstrip("+"))


def _table_free(body: str) -> str:
    """Drop pipe-table lines: the generated tables are right by construction."""
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("|"))


# Words that rank segments by growth, or move the mix toward some: claims about the model's
# own segment forecast, which ESLT's of 2026-10-07 made backwards. "Land and ISTAR lead
# growth" beside segment CAGRs where Land was fourth of five, and "a mix shift toward
# higher-margin Land" where Land's share of revenue fell from 28% to 27%.
LEADS_GROWTH = re.compile(r"\b(?:lead|leads|leading|drive|drives|driving|growth engines?|"
                          r"fastest[- ]growing|outgrow\w*|outpac\w*)\b", re.I)
GROWTH = re.compile(r"\bgrow|growth|CAGR", re.I)
MIX_TOWARD = re.compile(r"\b(?:mix|share) (?:shift\w*|mov\w*|tilt\w*|rotat\w*)\s+(?:\w+\s+){0,2}?"
                        r"(?:toward|towards|in favou?r of)\b|\bshift\w* (?:in mix |of mix )?"
                        r"(?:toward|towards)\b", re.I)
SEGMENT_CLAIM_MARGIN = 0.002    # CAGRs this close rank as a tie


def _segment_aliases(names: list[str]) -> dict[str, list[str]]:
    """How the text may name each segment: in full, or by a leading acronym ("ISTAR" for
    "ISTAR and EW", "C4I" for "C4I and Cyber")."""
    out = {}
    for n in names:
        first = n.split()[0]
        out[n] = [n] + ([first] if len(n.split()) > 1 and re.fullmatch(r"[A-Z0-9&]{3,}", first)
                        and sum(o.split()[0] == first for o in names) == 1 else [])
    return out


def segment_claims(text: str, v: Valuation) -> list[str]:
    """Claims about which segments lead growth, or which the mix moves toward, that the
    model's own segment forecast contradicts."""
    if len(v.segments) < 2 or not v.rows:
        return []
    total0 = v.base_revenue if v.segment_base else v.rows[0].revenue
    growth, share0, share1 = {}, {}, {}
    for name, series in v.segments.items():
        base = v.segment_base.get(name)
        start, span = (base, len(series)) if base and base > 0 else (series[0], len(series) - 1)
        if span <= 0 or not start or start <= 0 or series[-1] <= 0:
            return []
        growth[name] = (series[-1] / start) ** (1 / span) - 1
        share0[name], share1[name] = start / total0, series[-1] / v.rows[-1].revenue
    aliases = _segment_aliases(list(v.segments))

    def named(clause: str) -> list[str]:
        return [n for n, al in aliases.items()
                if any(re.search(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])", clause) for a in al)]

    ranked = sorted(growth, key=growth.get, reverse=True)
    out = []
    for sentence in SENTENCE_SPLIT.split(text):
        for clause in sentence.split(";"):
            said = named(clause)
            if said and LEADS_GROWTH.search(clause) and GROWTH.search(clause):
                cut = growth[ranked[len(said) - 1]] - SEGMENT_CLAIM_MARGIN
                behind = [n for n in said if growth[n] < cut]
                if behind:
                    faster = [n for n in ranked if n not in said and growth[n] > growth[behind[0]]]
                    out.append(f"the text says {' and '.join(said)} lead growth, but the model "
                               f"grows {behind[0]} {growth[behind[0]]:.1%} a year to "
                               f"{v.rows[-1].label}, behind "
                               + ", ".join(f"{n} ({growth[n]:.1%})" for n in faster[:3]))
            toward = MIX_TOWARD.search(clause)
            if toward:
                after = named(clause[toward.end():])
                falling = [n for n in after if share1[n] < share0[n] - 0.005]
                if falling:
                    n = falling[0]
                    out.append(f"the text says the mix shifts toward {n}, but in the model "
                               f"its share of revenue falls from {share0[n]:.0%} to "
                               f"{share1[n]:.0%} by {v.rows[-1].label}")
    return list(dict.fromkeys(out))


def own_text(body: str) -> str:
    """The prose's own claims: no tables, and no sentences about the Street, guidance or
    the market, which the Versus consensus paragraph quotes and are not the pitch's."""
    return "\n".join(s for s in SENTENCE_SPLIT.split(_table_free(body))
                     if not SKIP_WORDS.search(s))


HEADER_PRICE_RE = re.compile(r"(Intrinsic (?:Price|Value):\s*)" + CURRENCY + r"?\s*"
                             r"\d[\d,]*(?:\.\d+)?(?:\s*\([+\-−]?\d+(?:\.\d+)?%\))?")
HEADER_RATING_RE = re.compile(r"(Recommendation:\s*\**\s*)(?:BUY|HOLD|SELL)\b")


def fix_header(body: str, v: Valuation) -> str:
    """The one-line header with the computed intrinsic price, upside and rating. A line
    the reader sees first is mended, not deleted: BABA's of 2026-10-12 headed the pitch
    "Intrinsic Price: USD 116.70 (+5%) | Recommendation: HOLD" over a computed BUY."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if HEADER_PRICE_RE.search(line) and HEADER_RATING_RE.search(line):
            line = HEADER_PRICE_RE.sub(
                lambda m: f"{m.group(1)}{v.currency} {v.recommendation_value:,.2f} "
                          f"({pct(v.recommendation_upside)})", line)
            lines[i] = HEADER_RATING_RE.sub(lambda m: m.group(1) + v.rating, line)
    return "\n".join(lines)


def headline_contradictions(own: str, v: Valuation) -> list[tuple[str, str]]:
    """(sentence, problem) for each sentence giving a base case, an intrinsic price or a
    rating the computed valuation contradicts. These are the pitch's headline claims, so
    delivery deletes the sentence rather than ship it: BABA's of 2026-10-12 said
    "Recommendation: HOLD" at USD 116.70 beside a computed BUY at USD 263.51."""
    out: list[tuple[str, str]] = []
    cur = v.currency
    accepted_prices = [v.per_share, v.recommendation_value] + ([v.bull_per_share]
                                       if v.bull_per_share is not None else []) + ([v.cross_check_per_share]
                                       if v.cross_check_per_share is not None else [])
    if v.peers:
        accepted_prices += [x for x in (v.peers.value_ev_ebitda, v.peers.value_pe)
                            if x is not None]
    if v.downside_per_share is not None:
        accepted_prices.append(v.downside_per_share)
    for sentence in SENTENCE_SPLIT.split(own):
        # Not an amount with a scale: MSFT's "Base case DCF: enterprise value USD 1.90tn"
        # was read as a base case of 1.90 a share.
        for m in re.finditer(
                r"\b(?:base[- ]case|base case)\b(?:[^.\n]{0,60}?"
                r"(?:price|value|valuation)[^.\n]{0,25}?|[^.\n]{0,25}?)"
                r"(?:\$|USD\s*)(\d[\d,]*(?:\.\d+)?)(?!\d|\.\d|\s?(?:tn|bn|mn|[mbk]\b|"
                r"trillion|billion|million))", sentence, re.I):
            stated = _to_float(m.group(1))
            if not _close(stated, m.group(1), 1.0, v.per_share):
                out.append((sentence,
                            f'the text labels {m.group(1)} as the base case, but the model\'s '
                            f'base-case value is {cur} {v.per_share:,.2f}; the '
                            f'probability-weighted value is {v.weighted_per_share:,.2f}'
                            if v.weighted_per_share is not None else
                            f'the text labels {m.group(1)} as the base case, but the model\'s '
                            f'base-case value is {cur} {v.per_share:,.2f}'))
        # The other labelled values: GS on 2026-10-13 kept "Downside case: $524", "bull
        # case: $825" and "probability-weighted value $701" from a model run before its
        # payout revision, beside tables computing 502.21, 774.43 and 643.64.
        for label, pattern, computed in (
                ("downside case", r"\b(?:downside|bear)[- ]case", v.downside_per_share),
                ("bull case", r"\bbull[- ]case", v.bull_per_share),
                ("probability-weighted value", r"\bprobability[- ]weighted (?:value|price)",
                 v.weighted_per_share)):
            if computed is None:
                continue
            for m in re.finditer(pattern + r"\)?:?[^.\n$]{0,25}?(?:\$|USD\s*)(\d[\d,]*(?:\.\d+)?)"
                                 r"(?!\d|\.\d|\s?(?:tn|bn|mn|[mbk]\b|trillion|billion|million))",
                                 sentence, re.I):
                stated = _to_float(m.group(1))
                if not _close(stated, m.group(1), 1.0, computed):
                    out.append((sentence, f"the text gives the {label} as {m.group(1)}, but "
                                          f"the model computes {cur} {computed:,.2f}"))
        for m in INTRINSIC_RE.finditer(sentence):
            written = m.group(1)
            stated = _to_float(written)
            if not any(_close(stated, written, 1.0, p) for p in accepted_prices):
                out.append((sentence, f'the text gives an intrinsic/implied price of {written} '
                                      f'("{m.group(0).strip()}"), but the model computes '
                                      f'{cur} {v.per_share:,.2f}'))
        if CONDITIONAL.search(sentence):
            continue
        for pattern in RATING_RES:
            for m in pattern.finditer(sentence):
                if m.group(1) != v.rating:
                    out.append((sentence, f'the text says {m.group(1)} ("{m.group(0).strip()}"), '
                                          f'but the computed rating is {v.rating}'))
    return out


def check(body: str, v: Valuation, consensus: float | None = None,
          cover: str = "") -> list[str]:
    """Every figure in the prose that the computed valuation contradicts.

    `consensus` is the sell-side mean target, when there is one; `cover` is the one-liner
    and thesis pillars, whose multiples are checked with the body's.
    """
    problems: list[str] = []
    text = _table_free(body)
    cur = v.currency
    own = own_text(body)

    # Labels and directional language are claims too. Keep these checks tied to the
    # computed scenario and bridge fields so a correct number cannot be attached to the
    # wrong case or described with the wrong sign.
    problems += [problem for _, problem in headline_contradictions(own, v)]
    if v.minority > max(100.0, abs(v.enterprise_value) * 0.01) and re.search(
            r"\bno (?:material|significant) minorities\b|\bminorities? (?:are|is) immaterial\b",
            own, re.I):
        problems.append(f'the text calls minority interests immaterial, but the valuation '
                        f'bridge deducts {v.reporting_currency} {v.minority:,.0f}mn')
    # An equity-method row has no operating margin: "100bps of compression" there is a
    # NIM, ROE or cap rate (NEE's utility of 2026-10-12 read as 100.0% to 100.0%).
    if v.downside_rows and v.rows and v.family == "fcff":
        base_margin = v.rows[-1].ebit_margin
        bear_margin = v.downside_rows[-1].ebit_margin
        actual_bps = round((base_margin - bear_margin) * 10_000)
        for m in re.finditer(r"\b(\d{2,4})\s*(?:bp|bps|basis points?)\s*(?:margin )?"
                             r"(?:compression|cut|reduction)", own, re.I):
            stated = int(m.group(1))
            if abs(stated - actual_bps) > 50:
                problems.append(f'the text says the bear case has {stated}bps of margin '
                                f'compression, but the modeled change is {actual_bps}bps '
                                f'({base_margin:.1%} to {bear_margin:.1%})')
    if v.leverage and v.leverage.buyback_eps_change is not None:
        change = v.leverage.buyback_eps_change
        accretive = re.compile(r"\b(?:buybacks?|repurchases?)\b[^.\n]{0,100}\b(?:EPS[- ]?)?accretive\b", re.I)
        dilutive = re.compile(r"\b(?:buybacks?|repurchases?)\b[^.\n]{0,100}\b(?:EPS[- ]?)?dilutive\b", re.I)
        if change < -0.005 and accretive.search(own):
            problems.append(f'the text calls buybacks EPS-accretive, but the modeled borrowing '
                            f'case changes EPS by {change:.1%}')
        if change > 0.005 and dilutive.search(own):
            problems.append(f'the text calls buybacks EPS-dilutive, but the modeled borrowing '
                            f'case changes EPS by {change:+.1%}')
    if v.cross_check_per_share is not None and v.exit_multiple and v.exit_basis == "EBIT":
        last = v.rows[-1]
        equivalent = v.exit_multiple * last.ebit / last.ebitda if last.ebitda else None
        if equivalent:
            for m in re.finditer(r"\b(?:exit|cross[- ]check)\b[^.\n]{0,100}?"
                                 r"(?:EV\s*/\s*EBITDA|EBITDA multiple)[^0-9]{0,15}"
                                 r"(\d+(?:\.\d+)?)x", own, re.I):
                stated = float(m.group(1))
                if abs(stated / equivalent - 1) > MULTIPLE_TOLERANCE:
                    problems.append(f'the exit cross-check implies {equivalent:.1f}x FY2030 '
                                    f'EV/EBITDA, not {stated:.1f}x')
    # A sentence comparing implied market growth with the model should not call the
    # higher figure "modest" or otherwise claim it supports the lower-growth thesis.
    if v.implied_cagr is not None and v.model_cagr is not None and v.implied_cagr > v.model_cagr + 0.01:
        for sentence in own.splitlines():
            if (re.search(r"implied.{0,50}(?:growth|CAGR)", sentence, re.I)
                    and re.search(r"\b(?:modest|supports? (?:my|the) case|confirms?)\b", sentence, re.I)):
                problems.append("the text describes market-implied growth above the model's "
                                "growth as modest or supportive; explain that the market is "
                                "pricing in faster growth")
                break

    problems += segment_claims(own + "\n" + cover, v)

    accepted_upsides = [v.upside_pct, v.recommendation_upside] + ([v.bull_pct]
                                         if v.bull_pct is not None else []) + ([(v.cross_check_per_share / v.price - 1) * 100]
                                         if v.cross_check_per_share is not None else [])
    if v.downside_pct is not None:
        accepted_upsides.append(v.downside_pct)
    # The peer-implied values carry their own upside (AVGO: "+51% upside" at the median
    # EV/EBITDA), which is not a claim about the DCF.
    if v.peers:
        accepted_upsides += [(x / v.price - 1) * 100
                             for x in (v.peers.value_ev_ebitda, v.peers.value_pe)
                             if x is not None]

    def check_upside(value: float, snippet: str) -> None:
        if not any(abs(value - u) <= UPSIDE_TOLERANCE_PP for u in accepted_upsides):
            problems.append(f'the text states {snippet!r}, but the computed upside is '
                            f'{pct(v.upside_pct)}')

    for m in UPSIDE_AFTER_RE.finditer(own):
        value = _signed(m.group(1))
        if m.group(2).lower() == "downside":
            value = -abs(value)
        check_upside(value, m.group(0))
    for m in UPSIDE_BEFORE_RE.finditer(own):
        value = _signed(m.group(2))
        if m.group(1).lower() == "downside":
            value = -abs(value)
        check_upside(value, m.group(0))

    references = {"peer": v.peers.value if v.peers else None, "consensus": consensus}
    for m in COMPARE_RE.finditer(text):
        if re.search(r"\bnot\b|n't", m.group(0), re.I):
            continue
        target = m.group(2).lower()
        kind = "peer" if target == "peer" else "consensus"
        ref = references[kind]
        if not ref or abs(v.recommendation_value / ref - 1) < 0.02:
            continue
        says_up = m.group(1).lower() in COMPARE_UP
        if says_up != (v.recommendation_value > ref):
            where = "above" if v.recommendation_value > ref else "below"
            name = "the peer-multiple value" if kind == "peer" else "the consensus target"
            problems.append(f'the text says "{m.group(0).strip()}", but the DCF value of '
                            f"{cur} {v.recommendation_value:,.2f} is {where} {name} of {cur} {ref:,.2f}. "
                            "Reverse the comparison and the reasons given for it")

    for m in DIRECTION_RE.finditer(text):
        value, word = _signed(m.group(1)), m.group(2).lower()
        if value != 0 and (value > 0) != (word in UP_WORDS):
            problems.append(f'the text says "{m.group(0)}": a {"negative" if value < 0 else "positive"} '
                            "figure contradicts that direction word. Write the size without a sign "
                            f'and the direction in words (e.g. "{abs(value):g}% '
                            f'{"below" if value < 0 else "above"}")')

    # The cover's valuation language must make the same call as the computed rating.
    # "Fairly priced" on the cover of a SELL pitch reached RTX on 2026-10-06 even though
    # the recommendation card correctly said SELL.
    cover_language = cover.lower()
    contradiction = ((v.rating == "SELL" and
                      (FAIR_PRICE_RE.search(cover_language) or CHEAP_PRICE_RE.search(cover_language)))
                     or (v.rating == "BUY" and
                         (FAIR_PRICE_RE.search(cover_language) or EXPENSIVE_PRICE_RE.search(cover_language))))
    if contradiction:
        problems.append(f"the cover describes the shares in language that conflicts with the "
                        f"computed {v.rating} rating; rewrite the one_liner so it states why the "
                        "price is too high for SELL, too low for BUY, or fair only for HOLD")

    problems += _check_trading_multiple(text + "\n" + cover, v)
    # One contradiction is often quoted twice (header line and conclusion).
    return list(dict.fromkeys(problems))


def _check_trading_multiple(text: str, v: Valuation) -> list[str]:
    actual = v.current_ev_ebitda
    if not actual:
        return []
    # Yahoo's own figure is in the market data block too, on its own EV and EBITDA
    # (NEE, 2026-09-27: 18.98x against 15.0x here). Quoting either is not an error.
    accepted = [actual] + [q for q in (v.quoted_ev_ebitda,) if q]
    others = [p.symbol for p in v.peers.peers] if v.peers else []
    problems = []
    for sentence in SENTENCE_SPLIT.split(text):
        if (SKIP_WORDS.search(sentence) or NOT_TODAY.search(sentence)
                or any(re.search(rf"\b{re.escape(sym)}\b", sentence) for sym in others)):
            continue
        for m in TRADING_MULTIPLE_RE.finditer(sentence):
            written = m.group(1)
            if all(abs(_to_float(written) / a - 1) > MULTIPLE_TOLERANCE for a in accepted):
                quoted = (f" (Yahoo's own figure, on another basis, is "
                          f"{v.quoted_ev_ebitda:.1f}x)" if v.quoted_ev_ebitda else "")
                problems.append(f'the text says the stock is "{m.group(0).strip()}", but at '
                                f"today's price it trades at {actual:.1f}x trailing EBITDA"
                                f"{quoted}; cite {{{{ev_ebitda}}}}")
    return problems


# A figure typed right before the placeholder that holds the model's value for it:
# VST's "FY2030 FCF margin ~14% ({{fcf_margin:FY2030}}), FCF $3.5bn ({{fcf:FY2030}})"
# of 2026-09-29 printed "~14% (22.5%), FCF $3.5bn (USD 5.6bn)"; SPGI's of 2026-10-01
# "~28% (38.2%)". The writer's guess and the code's figure sat side by side, unchecked.
TYPED_BESIDE_RE = re.compile(
    r"(?<![\w.])(?P<typed>(?:~|c\.\s*)?" + CURRENCY + r"?\s*[+\-−]?\d[\d,]*(?:\.\d+)?"
    r"\s*(?:tn|trillion|bn|billion|mn|million|%|x)?)\s*\(\s*(?P<ph>\{\{[^{}]+\}\})\s*\)",
    re.I)
# Placeholders alone in brackets at a sentence's end, after words that do not name them:
# NVDA's of 2026-10-12 closed 51 sentences this way, the outline's figures copied after
# each claim, counter and signpost: "Signpost: BIS rule updates; China revenue line in
# quarterly reports ({{segment.Compute & Networking:FY2028}})" printed "(USD 646.0bn)".
ORPHAN_RE = re.compile(
    r"(?P<before>(?:[A-Za-z][\w'&/-]*\s+){0,5}[A-Za-z][\w'&/-]*)\s*"
    r"\(\s*(?P<phs>\{\{[^{}]+\}\}(?:\s*,\s*\{\{[^{}]+\}\})*)\s*\)"
    r"(?=\s*[.;]?\s*(?:\[#[^\]]*\])?\s*(?:\n|$|\*\*|[A-Z]))")
# What a figure is said against, not what it measures: "the bear case {{bear}} ({{bear_upside}})".
ORPHAN_KEEP = re.compile(r"upside|wacc_build|cross_check|consensus|eps_change", re.I)


def drop_orphan_placeholders(text: str) -> str:
    """The text without bracketed placeholders whose sentence does not say what they are."""
    from provenance import _words

    def name_words(ph: str) -> set[str]:
        return _words(re.sub(r"[{}_.:]+", " ", ph))

    def repl(m: re.Match) -> str:
        phs = re.findall(r"\{\{[^{}]+\}\}", m.group("phs"))
        said = _words(re.sub(r"[-/]", " ", m.group("before")))
        if (not said or any(ORPHAN_KEEP.search(p) for p in phs)
                or any(said & name_words(p) for p in phs)):
            return m.group(0)
        return m.group("before")

    return ORPHAN_RE.sub(repl, text)


# A paragraph's bold run-in lead. NVDA's of 2026-10-12 opened all three thesis pillars with
# the instructions' example lead, "**Prices are rising and subscribers are staying:**".
RUN_IN_LEAD = re.compile(r"^(?P<pre>\s*(?:[-*+]\s+)?)\*\*(?P<lead>[^*\n]+?):\*\*\s*", re.M)
REPEATED_LEAD_WORDS = 5


def drop_repeated_leads(text: str) -> str:
    """The text without a long run-in lead that opens more than one paragraph: a lead
    names what its paragraph argues, so one that fits several says nothing."""
    counts: dict[str, int] = {}
    for m in RUN_IN_LEAD.finditer(text):
        key = " ".join(m.group("lead").lower().split())
        counts[key] = counts.get(key, 0) + 1
    stale = {k for k, n in counts.items() if n > 1 and len(k.split()) >= REPEATED_LEAD_WORDS}
    if not stale:
        return text
    return RUN_IN_LEAD.sub(lambda m: m.group("pre") if " ".join(
        m.group("lead").lower().split()) in stale else m.group(0), text)


QUANTITY_RE = re.compile(
    r"^(?P<cur>" + CURRENCY + r")?\s*[+\-−]?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<unit>tn|trillion|bn|billion|mn|million|%|x)?$", re.I)


def _quantity(text: str) -> tuple[str, float, float] | None:
    """(kind, amount, slack) for "~14%", "$3.5bn", "USD 5.6bn" or "20.0x"; None for a bare
    number, which may be a year or a count."""
    m = QUANTITY_RE.match(re.sub(r"^(?:~|c\.\s*)", "", text.strip()))
    if not m:
        return None
    unit = (m.group("unit") or "").lower()
    number = _to_float(m.group("num"))
    if unit in ("%", "x"):
        return unit, number, _slack(m.group("num"), 1.0)
    if not (m.group("cur") or unit):
        return None
    scale = SCALE.get(unit, 1.0)
    return "money", number * scale, _slack(m.group("num"), scale)


def agrees_beside(typed: str, value: str) -> bool:
    """False when a typed figure disagrees with the placeholder value printed beside it."""
    a, b = _quantity(typed), _quantity(value)
    if a is None or b is None or a[0] != b[0]:
        return True
    kind, stated, slack = a
    actual = abs(b[1])
    if kind == "%":
        return abs(stated - actual) <= max(MARGIN_TOLERANCE_PP, slack)
    if kind == "x":
        return not actual or abs(stated / actual - 1) <= MULTIPLE_TOLERANCE
    return abs(stated - actual) <= max(AMOUNT_TOLERANCE * actual, slack)
