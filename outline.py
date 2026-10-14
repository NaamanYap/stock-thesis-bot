"""The outline: what the pitch will argue and which figures carry each point, checked by
code before any prose is written.

The facts-mode rewrites of SPGI (2026-10-03) wrote the whole pitch in one go and typed 297
figures against 11 citations; the code could only delete what was wrong, and took out a
third of the text. The outline moves the choosing of figures to a step that has no prose:
each point is a claim in words and the names of the figures behind it, so a typed number,
a name that does not exist, a figure for another period or an invalid assumption is caught
and sent back while it costs one line, not a paragraph. The prose is then written from the
checked outline.
"""

from __future__ import annotations

import json
import difflib
import re
from typing import Callable
from urllib.parse import urlparse

import facts

CONCLUSIONS_SECTION = "conclusions"
SECTIONS = ["Opening", "Investment Thesis", "Moat", "Competitive Dynamics and Strategy",
            "Revenue Growth and Margins", "Intrinsic Valuation", "Risks and Downside Case",
            "Conclusions", "Model Outputs"]
COMPUTED = re.compile(r"^(?P<op>share|ratio|change)\s*:\s*(?P<a>[\w]+)\s*/\s*(?P<b>[\w]+)$", re.I)
ASSUME = re.compile(r"^assume\s*:\s*(?P<id>[\w.-]+)$", re.I)
FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.S)

FIELD = """

# The outline, in the same header

Plan the pitch in the header too: for each section, the points you will make and the
figures that carry them, as an `outline` field beside `model` (and your `assumptions`, if
you have any, in the same header). The code checks it, and you then write the prose from
it:

```json
{
  "model": { ... },
  "outline": [
    {"section": "Opening", "points": [
      {"id": "O1", "claim": "A ratings and benchmarks duopoly whose growth this year came from a refinancing wave",
       "kind": "inference", "confidence": "medium",
       "figures": ["ratings_revenue_growth_2Q26", "ratings_share_of_revenue_2Q26"],
       "evidence": ["## [PRIMARY] fetch_page: <exact URL from the notes>"],
       "depends_on": [], "counterargument": "Issuance can stay weak if refinancing is delayed",
       "counter_evidence": ["## [PRIMARY] filing_section: <exact risk-factor block title from the notes>"],
       "counter_figures": [], "signpost": "New issuance and refinancing volumes",
       "signpost_evidence": [], "signpost_figures": ["issuance_volume_2Q26"],
       "distinguishing_evidence": "Refinancing volumes holding up while new issuance stays flat would favour my view over the counter"}
    ]},
    {"section": "Investment Thesis", "points": [ ... ]}
  ],
  "assumptions": [
    {"id": "price_rise", "value": "3-4%", "basis": "list prices have outrun inflation for a decade with no churn response"}
  ]
}
```

Rules, checked by code:
- One entry per section, in this order: """ + ", ".join(SECTIONS) + """ (the moat
  section under its company-specific heading). Three to six points a section; the
  Investment Thesis has your three pillars as its first three points.
- Give every point a unique short `id` and list the ids of the earlier points it builds on
  in `depends_on`. Do not create circular links or cite an id that does not exist.
- `evidence`, `counter_evidence`, and `signpost_evidence` are source titles or URLs from the
  research notes that support those statements. Copy a block label or page title exactly as
  written in the notes; do not invent a source. With facts mode off,
  `figures` may name computed model placeholders only; with facts mode on, the figure sheet's
  named facts may be used too.
- Every point needs at least one supporting source or figure in `evidence` / `figures`.
- `claim` is the point in words: what is true and why it matters. **No numbers in it** (years
  and quarters such as FY2025 or 2Q26 are fine): the numbers are the `figures`.
- Give each point a `kind` and a `confidence` (`high`, `medium`, or `low`, from the
  evidence, not from how strongly you feel). The kinds:
  - `fact`: reported, or read directly from one source. States no cause and no opinion: no
    "I", no "because", unless it attributes the cause ("management attributes ...").
  - `calculation`: arithmetic from reported figures. Also no cause and no opinion.
  - `inference`: interprets evidence; it needs at least two supports among `evidence`,
    `figures` and `depends_on`.
  - `assumption`: a forward-looking number or condition you introduce.
  - `hypothesis`: a causal link that is not shown. Word it as one ("may", "could", "if ...
    then") and give `test`: what would confirm or refute it.
  - `conclusion`: an implication for the business or the stock, with `depends_on` naming
    the points it follows from (at least two). The Conclusions section has at least two.
  Opinion ("I believe", "I expect") lives only in inference, assumption, hypothesis and
  conclusion points; a fact or calculation is stated plainly.
- Every point in Investment Thesis and Risks and Downside Case must also carry its strongest
  `counterargument` (not a straw man), an observable `signpost`, and
  `distinguishing_evidence`: what you would see if your view is right and not the counter.
  So must every inference, assumption or hypothesis in Revenue Growth and Margins (the
  forecast drivers). Each needs its own support: at least one
  source in `counter_evidence` or name in `counter_figures` for the counterargument, and one
  in `signpost_evidence` or `signpost_figures` for the signpost. The company's risk factors,
  a competitor's results and a news page in the notes are typical counter-evidence. These are real alternatives and measurable evidence to watch,
  not generic caveats. Keep numerical values in the figure-name lists, not in the prose fields.
- `figures` are names only: a figure sheet name (`revenue_growth_FY2025`), a model
  placeholder name (`ebit_margin:FY2030`, `implied_cagr`, `downside`), a computation of two
  sheet names (`share:a/b`, `ratio:a/b`, `change:a/b`), or `assume:id` for an assumption
  declared below. A point resting on the research notes rather than a figure gives an empty
  list and says the fact in its claim.
- A claim that names a period cites figures for that period; guidance is called guidance.
- `assumptions` are your own estimates, valid only as the rules above say: a judgement about
  the future or about something no filing reports, with a reason tied to an exact source
  title or URL in the research notes, and not one of your model's inputs. State the source
  and what it supports in `basis`; do not use memory as the basis.
- Before making points, review the dated recent-events list supplied below. Do not describe
  an event whose date has passed as upcoming. Every fact or inference point must name its
  supporting source in `evidence` or its source figure ids in `figures`; if support is absent,
  make it an explicitly labelled assumption with a sourced basis, or drop it.
- The Conclusions section names the key variable and the next dated event that will test it,
  when a date is publicly scheduled. If no event date is disclosed, say that plainly rather
  than inventing one.
"""


def parse(text: str) -> dict:
    match = FENCE.search(text)
    raw = match.group(1) if match else text[text.find("{"):text.rfind("}") + 1]
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"the outline is not valid JSON: {exc}") from exc
    if not isinstance(out, dict) or not isinstance(out.get("outline"), list):
        raise ValueError("the outline must be an object with an `outline` list")
    return out


def plain_field(placeholders: list[str] | tuple[str, ...] = ()) -> str:
    """The same claim map for the non-facts writer: its figures are model placeholders,
    while reported evidence is anchored to named research sources."""
    field = FIELD.replace('"ratings_revenue_growth_2Q26", "ratings_share_of_revenue_2Q26"',
                         '"tv_ev_ebitda", "implied_cagr"').replace(
        '"issuance_volume_2Q26"', '"tv_implied_growth"')
    field = re.sub(
        r"- `figures` are names only:.*?(?=\n- |\Z)",
        "- `figures` are computed model-placeholder names only. A reported fact belongs in the "
        "claim in words and is supported by a named source in `evidence`; do not type its "
        "numerical value in the outline.", field, flags=re.S)
    names = ", ".join(f"`{name}`" for name in placeholders)
    exact = "figures` are computed model-placeholder names only."
    return field.replace(exact, exact + " The exact names you may use are: "
                         + (names or "the computed placeholders supplied after valuation") + ".")


def _normalise_source(value: str) -> str:
    # The tier tag is the code's label, not the source's name: BABA's of 2026-10-12 cited
    # a "[SNIPPETS] web_search: ..." block as "[SECONDARY] web_search: ..." twelve times.
    value = re.sub(r"\[(?:PRIMARY|SECONDARY|SNIPPETS)\]", " ", value, flags=re.I)
    value = " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
    return value


def basis_names_source(basis: str, digest: str) -> bool:
    """An assumption's basis names a source and may go on to give its own reason:
    "latest earnings release: list prices have outrun inflation". The source alone is
    refused elsewhere, so the basis is matched clause by clause, not as a whole."""
    parts = [basis] + re.split(r"\s*(?:[;:,()]|\s[—–-]\s)\s*", basis)
    return any(source_matches(part, digest) for part in parts if part.strip())


def source_matches(source: str, digest: str) -> bool:
    """Match a citation to a normalized source label, page title, URL, or known data block."""
    raw = source.strip()
    if not raw:
        return False
    # The block header with a note after it: BABA's outline of 2026-10-12 cited
    # "## [PRIMARY] fetch_page: <url> (Chairman/CEO letter: ...)" 64 times, every one a
    # block in the notes, and shipped 70 problems.
    noted = re.match(r"^(.*\S)\s+\([^()]*\)$", raw, re.S)
    if noted and _source_match(noted.group(1), digest):
        return True
    return _source_match(raw, digest)


def _source_match(raw: str, digest: str) -> bool:
    norm = _normalise_source(raw)
    if norm in {"market data", "market data block"}:
        return True
    headers = re.findall(r"^##[^\n]*", digest, re.M)
    page_titles = re.findall(r"^# (?!# )[^\n]+", digest, re.M)
    labels = re.findall(r"^\[([^\]\n]+)\]", digest, re.M)
    titles = headers + page_titles + labels
    urls = re.findall(r"https?://[^\s)>]+", digest, re.I)
    for candidate in titles:
        candidate_norm = _normalise_source(candidate)
        if len(candidate_norm) < 4:
            continue
        if norm and norm == candidate_norm:
            return True
        source_words = {w for w in norm.split() if len(w) >= 3}
        title_words = set(candidate_norm.split())
        if len(source_words) >= 2 and source_words <= title_words:
            return True
    for url in urls:
        if raw.lower().rstrip("/") == url.lower().rstrip("/"):
            return True
        try:
            if "." in raw:
                host = urlparse(raw if raw.startswith(("http://", "https://")) else "https://" + raw).netloc.lower()
                other_host = urlparse(url).netloc.lower()
                if host.removeprefix("www.") == other_host.removeprefix("www."):
                    return True
        except ValueError:
            pass
    generic = {
        "latest 10 q": ("10 q", "10q"),
        "latest 10 k": ("10 k", "10k"),
        "latest earnings release": ("earnings release", "8 k", "8k"),
        "sec xbrl": ("sec xbrl",), "filing tables": ("filing tables",),
        "derived facts": ("derived facts",),
    }
    all_titles = _normalise_source(" ".join(titles))
    for alias, needles in generic.items():
        if norm.startswith(alias) and any(_normalise_source(n) in all_titles for n in needles):
            return True
    return False


def recent_events(digest: str, today=None, months: int = 18, limit: int = 24) -> str:
    """Extract recent events and announced near-term catalysts for planning."""
    from datetime import date, timedelta
    today = today or date.today()
    floor = today.year * 12 + today.month - months
    event_words = re.compile(r"guidance|outlook|results|read.?out|trial|approval|launch|"
                             r"announce|acquisition|patent|regulat|revenue|growth|forecast|"
                             r"quarter|earnings", re.I)
    date_re = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
    out, seen, source = [], set(), "research notes"
    for line in digest.splitlines():
        if line.startswith("## "):
            source = line.strip()
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\s*[•;]\s*", line.strip()):
            dates = list(date_re.finditer(sentence))
            if not dates or not event_words.search(sentence):
                continue
            for match in dates:
                year, month, day = map(int, match.groups())
                try:
                    event_date = date(year, month, day)
                except ValueError:
                    continue
                if (event_date.year * 12 + event_date.month < floor
                        or event_date > today + timedelta(days=months * 31)):
                    continue
                status = "UPCOMING" if event_date > today else "PAST"
                item = f"{status} {event_date.isoformat()} — {source}: {sentence[:360]}"
                if item not in seen:
                    seen.add(item)
                    out.append(item)
                break
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return "\n".join(out) if out else "No dated recent or scheduled event was extracted from the notes."


def validate_structure(outline: dict, is_model: Callable[[str], bool], digest: str,
                       placeholder_names: list[str] | tuple[str, ...] = ()
                       ) -> list[str]:
    """The claim graph, counterarguments and source anchors, without the facts database."""
    problems: list[str] = []
    for item in outline.get("assumptions") or []:
        if isinstance(item, dict) and item.get("id") and not basis_names_source(
                str(item.get("basis") or ""), digest):
            problems.append(f"assumption `{item['id']}` needs a basis that names a source "
                            "title or URL present in the research notes")
    sections = [str(s.get("section") or "") for s in outline.get("outline", [])
                if isinstance(s, dict)]
    expected = [s.lower() for s in SECTIONS]
    actual = [s.lower() for s in sections]
    if len(actual) != len(expected):
        problems.append(f"the outline needs exactly {len(expected)} entries in the V1 section order")
    elif any(actual[i] != expected[i] for i in range(len(expected)) if i != 2):
        problems.append("the outline sections must follow the V1 section order, with the moat "
                        "section allowed its company-specific heading")
    for section in outline.get("outline", []):
        if not isinstance(section, dict):
            continue
        label = str(section.get("section") or "")
        count = len(section.get("points") or []) if isinstance(section.get("points"), list) else 0
        if not 3 <= count <= 6:
            problems.append(f"section {label!r} needs three to six argument points")
    ids: dict[str, tuple[str, dict]] = {}
    order: dict[str, int] = {}
    bad_figures: list[tuple[str, str]] = []
    for i, (section, point) in enumerate(_points(outline)):
        pid = str(point.get("id") or "").strip()
        if not pid:
            problems.append(f"a point in {section} has no `id`")
        elif pid in ids:
            problems.append(f"point id `{pid}` is repeated")
        else:
            ids[pid], order[pid] = (section, point), i
        problems.extend(_label_problems(pid or "?", point))
    for pid, (section, point) in ids.items():
        deps = point.get("depends_on") or []
        if not isinstance(deps, list):
            problems.append(f"point `{pid}` needs `depends_on` as a list")
            deps = []
        for dep in deps:
            dep = str(dep)
            if dep not in ids:
                problems.append(f"point `{pid}` depends on unknown point `{dep}`")
            elif order[dep] >= order[pid]:
                problems.append(f"point `{pid}` must depend only on earlier point `{dep}`")
        if section.lower() in ("investment thesis", "risks and downside case"):
            for key in ("counterargument", "signpost"):
                if not str(point.get(key) or "").strip():
                    problems.append(f"point `{pid}` in {section} is missing `{key}`")
            # The fields named: NVO's of 2026-10-11 left ten counterarguments and signposts
            # unsupported after a re-ask that did not say where the support goes.
            if not (point.get("signpost_evidence") or point.get("signpost_figures")):
                problems.append(f"point `{pid}` needs evidence for its signpost: a source in "
                                "`signpost_evidence` or a figure name in `signpost_figures`")
            if not (point.get("counter_evidence") or point.get("counter_figures")):
                problems.append(f"point `{pid}` needs evidence for its counterargument: a source "
                                "in `counter_evidence` (a risk factor, competitor or news block "
                                "from the notes) or a figure name in `counter_figures`")
        if not (point.get("evidence") or point.get("figures")):
            problems.append(f"point `{pid}` needs supporting evidence")
        if str(point.get("kind") or "").strip().lower() == "assumption" \
                and not point.get("evidence"):
            problems.append(f"point `{pid}` is an assumption without a named source basis "
                            "in `evidence`")
        for key in ("evidence", "counter_evidence", "signpost_evidence"):
            entries = point.get(key) or []
            if not isinstance(entries, list):
                problems.append(f"point `{pid}` needs `{key}` as a list")
                continue
            for entry in entries:
                source = str(entry).strip()
                if source and not source_matches(source, digest):
                    problems.append(f"point `{pid}` cites `{source}` in `{key}`, which is absent "
                                    "from the research notes")
        for key in ("figures", "counter_figures", "signpost_figures"):
            entries = point.get(key) or []
            if not isinstance(entries, list):
                problems.append(f"point `{pid}` needs `{key}` as a list")
                continue
            for entry in entries:
                name = str(entry).strip().strip("{}").strip()
                if name and not is_model(name):
                    bad_figures.append((name, pid))
    if bad_figures:
        details = []
        for name, pid in bad_figures:
            near = difflib.get_close_matches(name, list(placeholder_names), n=1, cutoff=0.35)
            details.append(f"`{name}` in point `{pid}`" +
                           (f" (nearest valid placeholder: `{near[0]}`)" if near else ""))
        problems.append("reported facts belong in the claim with a source in `evidence`; "
                        "`figures` may contain computed placeholders only. Invalid names: "
                        + "; ".join(details))
    problems += kind_problems(outline)
    return list(dict.fromkeys(problems))


def _points(outline: dict):
    for sec in outline.get("outline") or []:
        if not isinstance(sec, dict):
            continue
        for p in sec.get("points") or []:
            if isinstance(p, dict):
                yield str(sec.get("section") or ""), p


KINDS = {"fact", "calculation", "inference", "assumption", "hypothesis", "conclusion"}
# Kinds that state what is reported, and so carry no opinion and no cause.
PLAIN_KINDS = {"fact", "calculation"}
# Sections whose views need a counterargument and what would tell them apart: the thesis,
# the risks and, for views rather than reported figures, the forecast drivers.
COUNTERED = ("investment thesis", "risks and downside case")
DRIVERS = "revenue growth and margins"
OPINION = re.compile(r"\b(?:I|I'm|I've|my|we|our|in my view)\b")
CAUSAL = re.compile(r"\b(?:because|due to|driven by|drives|drove|caused|causes|leads? to|led to|"
                    r"thanks to|as a result of|results? in|resulted in)\b", re.I)
ATTRIBUTED = re.compile(r"\b(?:attribut\w*|says?|said|according to|states?|stated|reports?|"
                        r"reported|management|the company|the 10-[KQ]|the filing|guid\w*)\b",
                        re.I)
HEDGED = re.compile(r"\b(?:may|might|could|if|would|whether|possibly|perhaps)\b", re.I)
CONFIDENCE = {"high", "medium", "low"}


def _label_problems(pid: str, point: dict) -> list[str]:
    kind = str(point.get("kind") or "").strip().lower()
    confidence = str(point.get("confidence") or "").strip().lower()
    out = []
    if kind not in KINDS:
        out.append(f"point `{pid}` needs `kind`: fact, calculation, inference, assumption, "
                   "hypothesis or conclusion")
    if confidence not in CONFIDENCE:
        out.append(f"point `{pid}` needs `confidence`: high, medium, or low")
    return out


def kind_problems(outline: dict) -> list[str]:
    """The skill's rules on claim types, beyond a valid label: what each kind must carry,
    opinion only where it is typed as opinion, and the counterargument and distinguishing
    evidence on every view the forecast rests on."""
    out: list[str] = []
    conclusions = 0
    for section, point in _points(outline):
        pid = str(point.get("id") or "?").strip()
        kind = str(point.get("kind") or "").strip().lower()
        claim = str(point.get("claim") or "")
        deps = point.get("depends_on") if isinstance(point.get("depends_on"), list) else []
        where = section.strip().lower()
        if kind in PLAIN_KINDS:
            if OPINION.search(claim):
                out.append(f"point `{pid}` is a {kind} but gives an opinion; state it plainly, "
                           "or type it inference, assumption, hypothesis or conclusion")
            if CAUSAL.search(claim) and not ATTRIBUTED.search(claim):
                out.append(f"point `{pid}` is a {kind} but states a cause; attribute it "
                           "(\"management attributes ...\") or type it a hypothesis with a test")
        elif kind == "inference":
            supports = sum(len(point.get(k) or []) for k in ("evidence", "figures")
                           if isinstance(point.get(k), list)) + len(deps)
            if supports < 2:
                out.append(f"point `{pid}` is an inference on one support; give at least two "
                           "among `evidence`, `figures` and `depends_on`, or type it as what "
                           "it is")
        elif kind == "hypothesis":
            if len(str(point.get("test") or "").split()) < 4:
                out.append(f"point `{pid}` is a hypothesis and needs `test`: what would "
                           "confirm or refute it")
            if not HEDGED.search(claim):
                out.append(f"point `{pid}` is a hypothesis worded as a fact; say \"may\", "
                           "\"could\" or \"if ... then\"")
        elif kind == "conclusion":
            if len(deps) < 2:
                out.append(f"point `{pid}` is a conclusion and needs `depends_on` naming at "
                           "least two points it follows from")
            if where == CONCLUSIONS_SECTION:
                conclusions += 1
        countered = where in COUNTERED or (where == DRIVERS and kind not in PLAIN_KINDS)
        if countered and kind != "conclusion":
            if where == DRIVERS and not str(point.get("counterargument") or "").strip():
                out.append(f"point `{pid}` in {section} is {'an' if (kind or 'view')[:1] in 'aeiou' else 'a'} {kind or 'view'} the forecast "
                           "rests on and needs its strongest `counterargument`")
            if len(str(point.get("distinguishing_evidence") or "").split()) < 5:
                out.append(f"point `{pid}` in {section} needs `distinguishing_evidence`: what "
                           "you would see if your view is right and the counterargument wrong")
    if any(s.strip().lower() == CONCLUSIONS_SECTION for s, _ in _points(outline)) \
            and conclusions < 2:
        out.append("the Conclusions section needs at least two `conclusion` points, each "
                   "naming in `depends_on` the points it follows from")
    return out


def marker_problems(body: str, outline: dict) -> list[str]:
    """Require each checked argument to be findable in the prose by its claim ID."""
    found = re.findall(r"\[#([A-Za-z0-9_-]+)\]", body)
    expected = {str(point.get("id") or "").strip(): point
                for _, point in _points(outline)
                if str(point.get("id") or "").strip()}
    problems = [f"the prose is missing the marker [#{pid}] for {point.get('claim', 'a point')}"
                for pid, point in expected.items() if pid not in found]
    problems.extend(f"the prose uses [#{pid}], which is not an outline point"
                    for pid in dict.fromkeys(found) if pid not in expected)
    return problems


def strip_invalid_markers(body: str, outline: dict | None) -> str:
    """Only claim IDs from the checked outline may appear in generated prose."""
    allowed = ({str(p.get("id") or "").strip() for _, p in _points(outline)}
               if isinstance(outline, dict) else set())
    return re.sub(r"\s*\[#([A-Za-z0-9_-]+)\]",
                  lambda m: m.group(0) if m.group(1) in allowed else "", body)


def drop_repeated_markers(body: str) -> str:
    """Each claim marker once, its first surviving use kept. Run on the delivered text,
    after the checks have deleted sentences: SPOT's of 2026-10-11 shipped eight "more than
    once" checks, bookkeeping no reader sees, and the first [#T3] was in a deleted sentence."""
    seen: set[str] = set()

    def keep(m: re.Match) -> str:
        if m.group(1) in seen:
            return ""
        seen.add(m.group(1))
        return m.group(0)
    return re.sub(r"\s*\[#([A-Za-z0-9_-]+)\]", keep, body)


_MARKER_STOP = {"about", "after", "also", "because", "could", "does", "from", "into",
                "more", "most", "over", "same", "that", "their", "there", "these",
                "they", "this", "through", "while", "which", "with", "would"}


def attach_obvious_markers(body: str, outline: dict) -> str:
    """Restore bookkeeping markers when the matching argument is clearly in the prose.

    IDs are stripped from the published PDF. Their absence should only be a check failure
    when the point itself is missing, not when the writer forgot an invisible tag.
    """
    found = set(re.findall(r"\[#([A-Za-z0-9_-]+)\]", body))
    paragraphs = list(re.finditer(r"(?m)^(?!\s*(?:#|\||[-*+]\s))(?=\S)(.+(?:\n(?!\s*(?:#|\||[-*+]\s|$)).+)*)", body))
    edits: list[tuple[int, str]] = []
    for section, point in _points(outline):
        pid = str(point.get("id") or "").strip()
        if not pid or pid in found:
            continue
        claim_words = {w.lower().rstrip("s") for w in re.findall(r"[A-Za-z]{4,}",
                       " ".join(str(point.get(k) or "") for k in
                                 ("claim", "counterargument", "signpost")))
                       if w.lower() not in _MARKER_STOP}
        if len(claim_words) < 2:
            continue
        section_match = re.search(r"(?im)^##\s+" + re.escape(section)
                                  + r"(?:\s*[:—-].*)?\s*$", body)
        scoped = [p for p in paragraphs if section_match and p.start() > section_match.end()
                  and not re.search(r"(?m)^##\s", body[section_match.end():p.start()])]
        ranked = []
        for p in scoped:
            words = {w.lower().rstrip("s") for w in re.findall(r"[A-Za-z]{4,}", p.group())
                     if w.lower() not in _MARKER_STOP}
            shared = claim_words & words
            score = len(shared) / len(claim_words)
            if len(shared) >= 2 and score >= 0.35:
                ranked.append((score, len(shared), p))
        if not ranked:
            continue
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
            continue
        para = ranked[0][2]
        edits.append((para.end(), f" [#{pid}]"))
        found.add(pid)
    for at, marker in sorted(edits, reverse=True):
        body = body[:at].rstrip() + marker + body[at:]
    return body


def validate(outline: dict, fx: facts.Facts, is_model: Callable[[str], bool],
             values: dict[str, str] | None = None, segments: list[str] | tuple = (),
             digest: str | None = None
             ) -> tuple[list[str], dict[str, facts.Assumption]]:
    """The problems with an outline, and the assumptions it keeps."""
    problems: list[str] = []
    sections = [str(s.get("section") or "") for s in outline["outline"] if isinstance(s, dict)]
    if len(sections) != len(SECTIONS):
        problems.append(f"the outline has {len(sections)} sections; it needs one for each of: "
                        + ", ".join(SECTIONS))
    elif any(sections[i].lower() != section.lower() for i, section in enumerate(SECTIONS)
             if i != 2):
        problems.append("the outline sections must follow the V1 section order, with the moat "
                        "section allowed its company-specific heading")
    assumptions, declared_problems = facts.assumptions_from(outline)
    assumptions, cite_as, refused = facts.reconcile_assumptions(assumptions, fx, values)
    problems += declared_problems + [p.replace(facts.ASSUMPTION_PROBLEM_END, "") for p in refused]
    for item in outline.get("assumptions") or []:
        if isinstance(item, dict) and item.get("id"):
            basis = str(item.get("basis") or "").strip()
            if not basis_names_source(basis, digest if digest is not None else fx.digest):
                problems.append(f"assumption `{item['id']}` needs a basis that names a source "
                                "title or URL present in the research notes")
    for key, ref in cite_as.items():
        problems.append(f"assumption `{key}` is a figure you can cite: use `{{{{{ref}}}}}` in "
                        f"place of `assume:{key}`")
    bad_names, typed, misused = [], [], []
    point_ids: dict[str, tuple[str, dict]] = {}
    point_order: dict[str, int] = {}
    for order, (section, point) in enumerate(_points(outline)):
        pid = str(point.get("id") or "").strip()
        if not pid:
            problems.append(f"a point in {section} has no `id`")
        elif pid in point_ids:
            problems.append(f"point id `{pid}` is repeated")
        else:
            point_ids[pid] = (section, point)
            point_order[pid] = order
        problems.extend(_label_problems(pid or "?", point))

    graph = {}
    for pid, (section, point) in point_ids.items():
        deps = [str(x).strip() for x in point.get("depends_on") or []]
        graph[pid] = deps
        for dep in deps:
            if dep not in point_ids:
                problems.append(f"point `{pid}` depends on unknown point `{dep}`")
            elif point_order[dep] >= point_order[pid]:
                problems.append(f"point `{pid}` must depend only on earlier outline points; "
                                f"`{dep}` comes later")
        if section.lower() in ("investment thesis", "risks and downside case"):
            for field in ("counterargument", "signpost"):
                if not str(point.get(field) or "").strip():
                    problems.append(f"point `{pid}` in {section} is missing `{field}`")
            if not (point.get("signpost_figures") or point.get("signpost_evidence")):
                problems.append(f"point `{pid}` in {section} needs evidence for its signpost")
    visiting, visited = set(), set()
    def visit(pid: str) -> bool:
        if pid in visiting:
            return True
        if pid in visited or pid not in graph:
            return False
        visiting.add(pid)
        cycle = any(visit(dep) for dep in graph[pid])
        visiting.remove(pid)
        visited.add(pid)
        return cycle
    if any(visit(pid) for pid in graph):
        problems.append("the outline's `depends_on` links contain a cycle")

    for section, p in _points(outline):
        kind = str(p.get("kind") or "").strip().lower()
        if kind in {"fact", "inference", "calculation"} and not (
                p.get("evidence") or p.get("figures")):
            problems.append(f"point `{p.get('id', '?')}` is a {kind} without source evidence; "
                            "name a source in `evidence` or source figure ids in `figures`")
        if kind == "assumption" and not p.get("evidence"):
            problems.append(f"point `{p.get('id', '?')}` is an assumption without a named "
                            "source basis in `evidence`")
        for text_key, refs_key in (("claim", "figures"), ("counterargument", "counter_figures"),
                                   ("signpost", "signpost_figures")):
            claim = str(p.get(text_key) or "")
            names = [str(n).strip().strip("{}").strip() for n in p.get(refs_key) or []]
            found = facts.typed(claim)
            if found:
                typed.append(f"\"{claim[:90]}\" ({', '.join(f.text for f, _ in found)})")
            refs = []
            for n in names:
                m = COMPUTED.match(n)
                a = ASSUME.match(n)
                if m:
                    pair = [facts.find(fx.by_id, m.group(k)) for k in ("a", "b")]
                    ok = all(pair)
                    if ok and facts._compute(m.group("op").lower(), *pair)[0] is None:
                        misused.append(f"`{n}` cannot be computed: "
                                       + facts._compute(m.group("op").lower(), *pair)[1])
                        continue
                elif a:
                    ok = a.group("id") in assumptions or a.group("id") in cite_as
                else:
                    ok = facts.find(fx.by_id, n) is not None or is_model(n)
                if not ok:
                    bad_names.append(f"`{n}` ({text_key} in {section})")
                elif not a and not is_model(n):
                    refs.append("{{" + n + "}}")
            if refs:
                for _, why in facts.misused(claim + " " + " ".join(refs) + ".", fx, segments):
                    misused.append(f"\"{claim[:80]}\": {why}")
        for key in ("evidence", "counter_evidence", "signpost_evidence"):
            for source in p.get(key) or []:
                source = str(source).strip()
                source_digest = digest if digest is not None else fx.digest
                if source and not source_matches(source, source_digest):
                    bad_names.append(f"source `{source}` ({key} in {section})")
    if typed:
        problems.append("these claims have typed numbers; put the figure's name in `figures` "
                        "and describe it in words: " + "; ".join(typed[:10]))
    if bad_names:
        problems.append("these figure names are not on the figure sheet, in your model's "
                        "placeholders or among your assumptions: " + ", ".join(bad_names[:15]))
    if misused:
        problems.append("these points cite a figure for something it is not: "
                        + "; ".join(misused[:8]))
    problems += kind_problems(outline)
    return problems, assumptions


def render(outline: dict) -> str:
    """The checked outline, as the prose request shows it."""
    lines = []
    for sec in outline.get("outline") or []:
        if not isinstance(sec, dict):
            continue
        lines.append(f"\n**{sec.get('section')}**")
        for p in sec.get("points") or []:
            if not isinstance(p, dict):
                continue
            def names_for(key):
                return ", ".join("{{" + str(n).strip().strip("{}") + "}}"
                                 for n in p.get(key) or [])
            names = names_for("figures")
            depends = ", ".join(str(n) for n in p.get("depends_on") or [])
            kind = str(p.get("kind") or "").strip().lower()
            lines.append(f"- ({p.get('id')}) " + (f"*{kind}* " if kind else "") + f"{p.get('claim')}"
                         + (f" [{names}]" if names else "")
                         + (f"; {'follows from' if kind == 'conclusion' else 'builds on'} "
                            f"{depends}" if depends else ""))
            if p.get("test"):
                lines.append(f"  Test: {p['test']}")
            if p.get("counterargument"):
                lines.append(f"  Counter: {p['counterargument']}"
                             + (f" [{names_for('counter_figures')}]" if p.get("counter_figures") else ""))
                if p.get("counter_evidence"):
                    lines.append("  Counter evidence: " + "; ".join(p["counter_evidence"]))
            if p.get("distinguishing_evidence"):
                lines.append(f"  What would tell them apart: {p['distinguishing_evidence']}")
            if p.get("signpost"):
                lines.append(f"  Signpost: {p['signpost']}"
                             + (f" [{names_for('signpost_figures')}]" if p.get("signpost_figures") else ""))
                if p.get("signpost_evidence"):
                    lines.append("  Signpost evidence: " + "; ".join(p["signpost_evidence"]))
            if p.get("evidence"):
                lines.append("  Evidence: " + "; ".join(p["evidence"]))
    return "\n".join(lines).strip()


PROSE = """# Step 2 of 2: the pitch

The code checked your outline:

{outline}

Now write the complete pitch from it: the ```json header again, with the same `model`,
`view` set to {rating}, and your `assumptions` exactly as in the outline, but without the
`outline` field, then the markdown. Each point becomes prose in its section, citing the figures listed for it, in
double braces, where the sentence states them; add another sheet figure only where the
argument needs it. Interpret the figures: what they show and why it matters for the
call. Preserve the strongest counterargument and the signpost for each thesis pillar and
each key risk; expand their reasoning in V1's full prose sections rather than compressing
the report into a list of claims. At the end of the first paragraph that makes each point,
add its ID in brackets, such as `[#O1]`; use each ID once. Write each point as the kind it is:
a fact or calculation plainly, with no "I"; an inference, assumption or conclusion in the
first person ("I expect", "I believe"); a hypothesis as one ("may", "if ... then") with its
test. Type no number anywhere in the prose. The very first character of your reply must
be the backtick opening the ```json fence."""

PROSE_STANDARD = """# Step 2 of 2: the pitch

The code checked your argument map:

{outline}

Now write the complete V1 pitch from it: return the ```json header with the same `model`
and `view` set to {rating}, followed by the full markdown report. Do not include the
`outline` field in the returned header. Turn each claim into fully developed prose in its
required V1 section. Preserve its evidence, strongest counterargument and measurable
signpost. At the end of the first paragraph that makes each point, add its ID in brackets,
such as `[#O1]`; use each ID once. Write each point as the kind it is: a fact or calculation
plainly, with no "I"; an inference, assumption or conclusion in the first person ("I
expect", "I believe"); a hypothesis as one ("may", "if ... then") with its test. Cite factual figures to the named primary source in the research notes; never
invent a source. Cite computed values through the model placeholders. Keep V1's full depth,
voice, section structure and target length. The very first character must be the backtick
opening the ```json fence."""
