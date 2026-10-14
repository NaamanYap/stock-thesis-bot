"""Save what went into a pitch and what came out, and replay the code checks on it.

Every check so far was tested on sentences written to match it, and the first real test
of each one was the next live pitch: five rounds of fixes, each followed by a pitch that
found a phrasing the checks missed. A replay file holds a real run — the market data, the
research notes the writer read, the peers and every raw write attempt — so a change to
the checks can be run against every pitch the bot has written before it ships.

    python replay.py run NEE                 a full pitch here, saved to replays/ with its PDF
    python replay.py check [files...]        the code checks on saved pitches (default: replays/)
    python replay.py golden [--update]       the flags on every saved pitch against the
                                             reviewed list in replays/expected_flags.json
    python replay.py rewrite FILE            the steps after research rerun live on a saved
                                             pitch's research, to replays/rewrites/
    python replay.py assumptions FILE [OUT]   export editable valuation inputs from a saved run
    python replay.py revalue FILE ASSUMPTIONS compare the saved valuation with your assumptions
    python replay.py ledger [files...]       every figure in each delivered pitch and its
                                             outcome, against replays/figure_labels.json
    python replay.py regather FILE           fresh notes from gather.py for a saved pitch's
                                             company, then `rewrite` on them, to replays/rewrites/
    python replay.py score [--live]          the regression suite (replays/suite.json) scored
                                             by today's checks, or rewritten live and scored,
                                             against the last score; history in scores.csv
    python replay.py compare FILE [FILE...]  notes, figures, problems, rating and stage times
                                             of saved pitches, side by side
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import config
import market_data
import outline
from market_data import MarketData

REPLAY_DIR = config.BASE_DIR / "replays"
EXPECTED = REPLAY_DIR / "expected_flags.json"
REPLAY_NAME = re.compile(r"^[A-Z0-9.\-]+_\d{8}T\d{6}\.json(?:\.gz)?$")
VERSION = 1
# The model-input warnings that judge the forecast against the company's record, listed
# with the figure flags so a change to either shows up in `golden`.
GOLDEN_WARNINGS = ("should build revenue", "add back D&A", "jump in cash conversion")


def record(data: MarketData, digest: str, peer_cache: dict, writes: list[str],
           revision: str | None = None, facts_mode: bool = False,
           outline: dict | None = None) -> dict:
    """Everything needed to rerun `_finalise` on this pitch without the network."""
    peers = {",".join(key): [dataclasses.asdict(p) for p in found]
             for key, found in peer_cache.items()}
    return {"version": VERSION, "symbol": data.symbol,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "data": dataclasses.asdict(data), "digest": digest, "peers": peers,
            "writes": list(writes), "revision": revision, "facts_mode": facts_mode,
            "outline": outline}


def save(replay: dict, directory: Path | None = None) -> Path:
    directory = directory or REPLAY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = replay["saved"].replace(":", "").replace("-", "")
    path = directory / f"{replay['symbol']}_{stamp}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(replay, fh)
    return path


def load(path: Path) -> dict:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        replay = json.load(fh)
    fields = dict(replay["data"])
    if fields.get("firm_targets") is not None:
        fields["firm_targets"] = [market_data.FirmTarget(**f) for f in fields["firm_targets"]]
    fields["history"] = [tuple(h) for h in fields.get("history") or []]
    replay["data"] = MarketData(**fields)
    replay["peers"] = {key: [market_data.PeerMultiples(**p) for p in found]
                       for key, found in (replay.get("peers") or {}).items()}
    return replay


def _facts(replay: dict):
    """The tagged notes, for a pitch written in facts mode; None for one written before."""
    if not replay.get("facts_mode"):
        return None
    import facts
    import figure_sheet
    return figure_sheet.attach(facts.build(replay["digest"], replay["data"].prompt_block),
                               replay["data"])


def replay_files() -> list[Path]:
    """The saved pitches in replays/, leaving out the flag list and anything else there."""
    return [p for p in sorted(REPLAY_DIR.glob("*.json*")) if REPLAY_NAME.match(p.name)]


def _attempts(replay: dict) -> list[tuple[str, str]]:
    texts = [(f"write {i}", t) for i, t in enumerate(replay["writes"], 1)]
    if replay.get("revision"):
        texts.append(("revision", replay["revision"]))
    return texts


def golden(replay: dict) -> list[str]:
    """One line per wrong figure and per forecast warning, on every saved attempt.

    Each line was read against the pitch and judged a real error before it went into
    expected_flags.json; `golden` fails on any line added or lost, so a change to the checks
    is judged on real pitches, not on sentences written to pass it.
    """
    import pitch as pitch_mod

    data, digest = replay["data"], replay["digest"]
    out = []
    # Judged against the numbers the writer was shown: every saved pitch predates the glide.
    # Production's house discount rate, whatever the caller (the offline tests) has set.
    saved = config.GLIDE_YEARS, config.HOUSE_WACC
    config.GLIDE_YEARS, config.HOUSE_WACC = 0, True
    try:
        return _golden_lines(_attempts(replay), data, digest, replay, pitch_mod)
    finally:
        config.GLIDE_YEARS, config.HOUSE_WACC = saved


def _golden_lines(attempts, data, digest, replay, pitch_mod) -> list[str]:
    out = []
    for label, text in attempts:
        try:
            headline, body = pitch_mod._split_headline(pitch_mod._clean(text))
        except pitch_mod.PitchError:
            continue
        if replay.get("outline"):
            # As the live write does: invalid and repeated markers out first.
            body = outline.strip_invalid_markers(body, replay["outline"])
            body = outline.attach_obvious_markers(body, replay["outline"])
        model = headline.get("model")
        key = ",".join(pitch_mod.peer_key(model))
        result, _ = pitch_mod._finalise(headline, body, data, replay["peers"].get(key, []),
                                        digest=digest, fx=_facts(replay))
        for fl in result.flags + result.cover_flags:
            if fl.wrong:
                out.append(f"{label} | {fl.kind} | {fl.figure.text} | {fl.sentence.strip()[:90]}")
        for w in (result.valuation.warnings if result.valuation else []):
            if w.startswith(GOLDEN_WARNINGS) or any(g in w for g in GOLDEN_WARNINGS):
                out.append(f"{label} | warning | {w[:90]}")
    return out


def golden_diff(update: bool = False) -> dict[str, tuple[list[str], list[str]]]:
    """{replay file: (lines no longer flagged, lines newly flagged)} against EXPECTED."""
    expected = json.loads(EXPECTED.read_text(encoding="utf-8")) if EXPECTED.exists() else {}
    found = {p.name: golden(load(p)) for p in replay_files()}
    if update:
        EXPECTED.write_text(json.dumps(found, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        return {}
    diff = {}
    for name, lines in found.items():
        old = expected.get(name, [])
        lost, new = [l for l in old if l not in lines], [l for l in lines if l not in old]
        if lost or new:
            diff[name] = (lost, new)
    return diff


def shipped(replay: dict):
    """(label, Pitch) for the attempt the bot delivered: the best write, as `generate`
    ranks them, or the revision when it passed the code checks as well. None when no
    attempt computed."""
    import pitch as pitch_mod
    import outline

    data, digest = replay["data"], replay["digest"]
    best = None
    for label, text in _attempts(replay):
        try:
            headline, body = pitch_mod._split_headline(pitch_mod._clean(text))
        except pitch_mod.PitchError:
            continue
        model = headline.get("model")
        key = ",".join(pitch_mod.peer_key(model))
        result, problems = pitch_mod._finalise(headline, body, data,
                                               replay["peers"].get(key, []), digest=digest,
                                               fx=_facts(replay))
        if replay.get("outline"):
            problems += outline.marker_problems(body, replay["outline"])
        if label == "revision":
            if (best and result.valuation and len(problems) <= len(best[2])
                    and len(result.removed) <= len(best[1].removed)):
                best = (label, result, problems)
        elif best is None or pitch_mod._rank(result, problems) < pitch_mod._rank(best[1], best[2]):
            best = (label, result, problems)
    if not best or not best[1].valuation:
        return None
    try:
        result = pitch_mod._prepare_delivery(best[1], best[2], replay.get("outline"),
                                             _primary_sources_read(replay))
    except pitch_mod.PitchError:
        return None
    return best[0], result


def _primary_sources_read(replay: dict) -> int | None:
    """Use the recorded read count, or infer distinct fetched primary pages for old replays."""
    count = replay.get("primary_sources_read")
    if isinstance(count, int):
        return count
    digest = replay.get("digest") or ""
    urls = re.findall(r"(?m)^## \[PRIMARY\] fetch_page: (https?://\S+)", digest)
    unique = set()
    for url in urls:
        parsed = urlsplit(url)
        unique.add((parsed.scheme.lower(), (parsed.hostname or "").lower(),
                    parsed.path.rstrip("/")))
    return len(unique) if urls else None


LABELS = REPLAY_DIR / "figure_labels.json"
OUTCOMES = ("sourced", "untraced", "wrong", "removed")


def _ledger_key(figure_text: str, sentence: str) -> str:
    return f"{figure_text} | {' '.join(sentence.split())[:70]}"


def ledger(replay: dict) -> tuple[str, list[tuple[str, str, str]]]:
    """Every figure in the delivered text with what the checks made of it, as (key,
    outcome, where) where `where` is "body" or "cover" and the outcome one of OUTCOMES.

    `golden` holds only the figures the checks call wrong, so a figure they passed in
    error, or marked † although its source was in the notes, was never measured.
    """
    import provenance

    saved = config.GLIDE_YEARS, config.HOUSE_WACC
    config.GLIDE_YEARS, config.HOUSE_WACC = 0, True
    try:
        found = shipped(replay)
        if found is None:
            return "", []
        label, result = found
        out = []
        if result.pool is None:
            # Facts mode: every figure left is a cited fact, a model value or a declared
            # assumption; what the writer typed was taken out.
            for where, text in (("body", result.prose), ("cover", result.cover)):
                for sentence in provenance.SENTENCE.split(text):
                    out += [(_ledger_key(f.text, sentence), "sourced", where)
                            for f in provenance.figures(sentence)]
        else:
            for where, text in (("body", result.prose), ("cover", result.cover)):
                for f, sentence, kind in provenance.outcomes(text, result.pool):
                    outcome = kind if kind in ("sourced", "untraced") else "wrong"
                    out.append((_ledger_key(f.text, sentence), outcome, where))
        out += [(_ledger_key("", sentence), "removed", "body")
                for sentence, _ in result.removed]
        return label, out
    finally:
        config.GLIDE_YEARS, config.HOUSE_WACC = saved


def ledger_report(paths: list[Path]) -> tuple[dict, list[str]]:
    """Totals by outcome, and against the hand labels in figure_labels.json: the misses (a
    figure labelled untraced or wrong that the checks passed), the false alarms (labelled
    sourced, flagged) and the labels whose figure is no longer in the text."""
    labels = json.loads(LABELS.read_text(encoding="utf-8")) if LABELS.exists() else {}
    totals = {o: 0 for o in OUTCOMES} | {"miss": 0, "false alarm": 0, "agree": 0}
    lines = []
    for path in paths:
        label, rows = ledger(load(path))
        counts = {o: sum(1 for _, out, _ in rows if out == o) for o in OUTCOMES}
        for o in OUTCOMES:
            totals[o] += counts[o]
        lines.append(f"== {path.name} ({label or 'no computed attempt'}): "
                     + ", ".join(f"{counts[o]} {o}" for o in OUTCOMES))
        truth = labels.get(path.name, {})
        seen = set()
        for key, outcome, _ in rows:
            if key not in truth or key in seen:
                continue
            seen.add(key)
            want = truth[key]
            if want == outcome:
                totals["agree"] += 1
            elif outcome == "sourced":
                totals["miss"] += 1
                lines.append(f"  miss ({want}, passed): {key}")
            else:
                totals["false alarm"] += 1
                lines.append(f"  false alarm (labelled {want}, {outcome}): {key}")
        for key in truth.keys() - {k for k, _, _ in rows}:
            lines.append(f"  label not in the text: {key}")
    return totals, lines


def check(replay: dict) -> list[tuple[str, list[str]]]:
    """The problems today's code finds in each saved write attempt, in order."""
    import pitch as pitch_mod
    import outline

    data, digest = replay["data"], replay["digest"]
    out = []
    for label, text in _attempts(replay):
        try:
            headline, body = pitch_mod._split_headline(pitch_mod._clean(text))
        except pitch_mod.PitchError as exc:
            out.append((label, [f"unparsable: {exc}"]))
            continue
        if replay.get("outline"):
            # As the live write does: invalid and repeated markers out first.
            body = outline.strip_invalid_markers(body, replay["outline"])
            body = outline.attach_obvious_markers(body, replay["outline"])
        model = headline.get("model")
        key = ",".join(pitch_mod.peer_key(model))
        result, problems = pitch_mod._finalise(
            headline, body, data, replay["peers"].get(key, []), digest=digest,
            fx=_facts(replay))
        if replay.get("outline"):
            problems += outline.marker_problems(body, replay["outline"])
        try:
            result = pitch_mod._prepare_delivery(
                result, problems, replay.get("outline"), _primary_sources_read(replay))
            out.append((label, result.checks))
        except pitch_mod.PitchError as exc:
            out.append((label, [str(exc)]))
    return out


async def _run(query: str) -> None:
    import openai
    import pdf_render
    import pitch as pitch_mod

    logging_setup()
    data = await asyncio.to_thread(market_data.fetch, query)
    client = openai.AsyncOpenAI(base_url=config.NIM_BASE_URL, api_key=config.NVIDIA_API_KEY,
                                max_retries=0, timeout=config.REQUEST_TIMEOUT_S)
    try:
        result = await pitch_mod.generate(data, client)
    finally:
        await client.close()
        await pitch_mod.close_clients()
    path = save(result.replay)
    pdf = await asyncio.to_thread(pdf_render.build, result, data)
    target = REPLAY_DIR / pdf.name
    pdf.replace(target)
    print(f"replay: {path}\npdf: {target}\nchecks: {result.checks}")


REWRITE_DIR = REPLAY_DIR / "rewrites"


FILING_BLOCK = re.compile(r"^## \[PRIMARY\] fetch_page: (?P<url>\S+)\n\n\[(?P<label>latest [^\]\n]+)\]",
                          re.M)


def _add_filing_tables(digest: str, symbol: str = "") -> str:
    """A pitch saved before facts mode kept its filings' text only. Their tables are read
    again from the same SEC URLs (a filing never changes) and put after each filing, as
    research does now. Without SEC_USER_AGENT the notes stay as they were."""
    import logging
    import research_tools
    import filing_tables
    import sec_xbrl

    if not config.SEC_USER_AGENT:
        logging.warning("SEC_USER_AGENT is not set: rewriting without the filings' tables")
        return digest
    out = digest
    for m in reversed(list(FILING_BLOCK.finditer(digest))):
        page = research_tools._fetch_soup(m.group("url"))
        if isinstance(page, str):
            logging.warning("could not read %s again: %s", m.group("url"), page[:120])
            continue
        tables = filing_tables.render(filing_tables.tables(page[1]), config.FILING_TABLE_CHARS)
        if not tables:
            continue
        nxt = digest.find("\n## [", m.end())
        at = nxt if nxt >= 0 else len(digest)
        block = f"\n\n## [PRIMARY] filing_tables: {m.group('label')}\n\n{tables}"
        out = out[:at] + block + out[at:]
    # The XBRL facts as they stand today, which may include filings after the pitch.
    xbrl = sec_xbrl.read(symbol) if symbol else ""
    if xbrl:
        first = out.find("\n\n## [SECONDARY]")
        at = first if first >= 0 else out.find("\n\n## [SNIPPETS]")
        at = at if at >= 0 else len(out)
        out = out[:at] + f"\n\n## [PRIMARY] sec_xbrl: company facts\n\n{xbrl}" + out[at:]
    # Under the same cap as research: the notes run filings first and snippets last, so
    # whole blocks come off the end, as _build_digest cuts them.
    while len(out) > config.DIGEST_MAX_CHARS:
        last = out.rfind("\n\n## [")
        if last <= 0:
            out = out[:config.DIGEST_MAX_CHARS]
            break
        out = out[:last]
    return out


async def _rewrite(path: Path) -> Path:
    """The model, the prose, the checks and the review, live, on the research a saved
    pitch already gathered: the same notes under the current code, without the 20 minutes
    of research, so two versions of the writing steps can be judged on identical input."""
    import openai
    import pdf_render
    import pitch as pitch_mod

    logging_setup()
    saved = load(path)
    data = saved["data"]
    digest = saved["digest"]
    if config.FACTS_MODE and "## [PRIMARY] filing_tables:" not in digest:
        digest = await asyncio.to_thread(_add_filing_tables, digest, data.symbol)
    peer_cache = {tuple(key.split(",")): found for key, found in saved["peers"].items()}
    client = openai.AsyncOpenAI(base_url=config.NIM_BASE_URL, api_key=config.NVIDIA_API_KEY,
                                max_retries=0, timeout=config.REQUEST_TIMEOUT_S)
    try:
        result = await pitch_mod.write(data, digest, [], client, peer_cache=peer_cache)
    finally:
        await client.close()
        await pitch_mod.close_clients()
    out = save(result.replay, REWRITE_DIR)
    pdf = await asyncio.to_thread(pdf_render.build, result, data)
    target = REWRITE_DIR / pdf.name
    pdf.replace(target)
    print(f"replay: {out}\npdf: {target}\nchecks: {result.checks}")
    return out


async def _regather(path: Path) -> None:
    """`rewrite`, on notes gathered now by gather.py rather than the saved ones: the same
    market data and peers, so the research is the one thing that differs (RESEARCH_MODE)."""
    import time
    import openai
    import gather
    import pdf_render
    import pitch as pitch_mod

    logging_setup()
    saved = load(path)
    data = saved["data"]
    peer_cache = {tuple(key.split(",")): found for key, found in saved["peers"].items()}
    client = openai.AsyncOpenAI(base_url=config.NIM_BASE_URL, api_key=config.NVIDIA_API_KEY,
                                max_retries=0, timeout=config.REQUEST_TIMEOUT_S)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        started = time.monotonic()
        digest, sources = await gather.gather(client, data, None, usage)
        pitch_mod._timed(usage, "research", started)
        usage["research_path"] = "gather"
        result = await pitch_mod.write(data, digest, sources, client, usage=usage,
                                       peer_cache=peer_cache)
    finally:
        await client.close()
        await pitch_mod.close_clients()
    out = save(result.replay, REWRITE_DIR)
    pdf = await asyncio.to_thread(pdf_render.build, result, data)
    target = REWRITE_DIR / pdf.name
    pdf.replace(target)
    print(f"replay: {out}\npdf: {target}\nchecks: {result.checks}")


def summary(replay: dict) -> dict:
    """What `compare` lists for one saved pitch, judged by today's checks."""
    import pitch as pitch_mod

    digest = replay["digest"]
    blocks = re.split(r"\n\n(?=## \[)", digest)
    primary = sum(len(b) for b in blocks if b.startswith("## [PRIMARY]"))
    out = {"notes": len(digest), "primary": primary / max(1, len(digest)),
           "timings": replay.get("timings"), "path": replay.get("research_path", ""),
           "review": replay.get("review_status", "")}
    saved = config.GLIDE_YEARS, config.HOUSE_WACC
    config.GLIDE_YEARS, config.HOUSE_WACC = 0, True
    try:
        found = shipped(replay)
        problems = dict(check(replay))
    finally:
        config.GLIDE_YEARS, config.HOUSE_WACC = saved
    if found:
        label, result = found
        flagged = sum(1 for fl in result.flags + result.cover_flags if fl.wrong)
        # As the bot ships it: wrong sentences deleted, untraced figures marked.
        result.checks = list(problems.get(label, []))
        pitch_mod._repair(result)
        out.update(attempt=label, rating=result.valuation.rating,
                   value=result.valuation.per_share, currency=replay["data"].currency,
                   flagged=flagged, removed=len(result.removed), untraced=result.unverified,
                   problems=len(result.checks))
    return out


SUITE = REPLAY_DIR / "suite.json"
SCORES = REPLAY_DIR / "scores.csv"
SCORE_FIELDS = ("date", "mode", "case", "file", "problems", "removed", "untraced", "flagged",
                "rating", "value", "currency")
# What each column counts, lower is better, for the totals line.
SCORE_TOTALS = ("problems", "removed", "untraced", "flagged")


def suite_cases() -> list[Path]:
    """The suite's saved pitches. A case not in replays/ is looked for in replays/local_runs/,
    where runs that are not yet on the reviewed golden list are kept."""
    names = json.loads(SUITE.read_text(encoding="utf-8"))["cases"]
    out = []
    for name in names:
        path = REPLAY_DIR / name
        out.append(path if path.exists() else REPLAY_DIR / "local_runs" / name)
    return out


def _case(name: str) -> str:
    return name.split("_", 1)[0]


def _last_scores(mode: str) -> dict[str, dict]:
    """The latest scored row of each case in this mode, from scores.csv."""
    import csv
    if not SCORES.exists():
        return {}
    last: dict[str, dict] = {}
    with SCORES.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("mode") == mode:
                last[row["case"]] = row
    return last


def _score_row(path: Path, mode: str, today: str) -> dict:
    s = summary(load(path))
    return {"date": today, "mode": mode, "case": _case(path.name), "file": path.name,
            "problems": s.get("problems", ""), "removed": s.get("removed", ""),
            "untraced": s.get("untraced", ""), "flagged": s.get("flagged", ""),
            "rating": s.get("rating", "none"),
            "value": f"{s['value']:.2f}" if "value" in s else "",
            "currency": s.get("currency", "")}


def score(live: bool = False) -> str:
    """The regression suite's scorecard. Offline, today's checks on each case's saved drafts:
    what a code change does to the checks, in seconds and with no model calls. Live, the
    writer rerun on each case's saved research (`rewrite`) and the rewrites scored: what a
    prompt or writing change does, at a pitch's cost per case. Each row is compared with the
    last score in the same mode and appended to scores.csv."""
    import csv
    mode = "live" if live else "offline"
    cases = suite_cases()
    missing = [p.name for p in cases if not p.exists()]
    if missing:
        return "missing suite case(s): " + ", ".join(missing)
    saved_values = {}
    if live:
        rewritten = []
        for path in cases:
            saved_values[_case(path.name)] = summary(load(path)).get("value")
            rewritten.append(asyncio.run(_rewrite(path)))
        cases = rewritten
    today = datetime.now().date().isoformat()
    before = _last_scores(mode)
    rows = [_score_row(p, mode, today) for p in cases]

    def delta(row: dict, key: str) -> str:
        old = before.get(row["case"], {}).get(key, "")
        if str(row[key]) == "" or old == "":
            return str(row[key])
        change = int(row[key]) - int(old)
        return f"{row[key]} ({change:+d})" if change else str(row[key])

    head = ["| case | file | open problems | removed | † untraced | wrong figures | rating "
            "| value/share |" + (" vs saved |" if live else ""),
            "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |" + (" ---: |" if live else "")]
    lines = []
    for row in rows:
        value = f"{row['currency']} {float(row['value']):,.2f}" if row["value"] else "—"
        old_rating = before.get(row["case"], {}).get("rating")
        rating = (f"{row['rating']} (was {old_rating})"
                  if old_rating and old_rating != row["rating"] else row["rating"])
        line = (f"| {row['case']} | {row['file']} | {delta(row, 'problems')} | "
                f"{delta(row, 'removed')} | {delta(row, 'untraced')} | {delta(row, 'flagged')} | "
                f"{rating} | {value} |")
        if live:
            saved = saved_values.get(row["case"])
            moved = (f"{float(row['value']) / saved - 1:+.0%}"
                     if saved and row["value"] else "—")
            line += f" {moved} |"
        lines.append(line)
    totals = {k: sum(int(r[k]) for r in rows if str(r[k]) != "") for k in SCORE_TOTALS}
    old_totals = {k: sum(int(before[r["case"]][k]) for r in rows
                         if r["case"] in before and before[r["case"]].get(k, "") != "")
                  for k in SCORE_TOTALS}
    total_line = (f"**Total ({mode}, {len(rows)} cases):** "
                  + ", ".join(f"{k} {totals[k]}"
                              + (f" ({totals[k] - old_totals[k]:+d})" if before else "")
                              for k in SCORE_TOTALS))
    new_file = not SCORES.exists()
    with SCORES.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    since = (f"Changes are against the last {mode} score." if before
             else f"First {mode} score: no earlier one to compare with.")
    return "\n".join(head + lines + ["", total_line, since, f"Appended to {SCORES.name}."])


def compare(paths: list[Path]) -> str:
    """A markdown table, one row per saved pitch."""
    rows = ["| file | notes | PRIMARY | attempt | wrong figures | removed | † untraced "
            "| open problems | rating | value/share | review | stage times |",
            "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |"]
    for path in paths:
        s = summary(load(path))
        times = ", ".join(f"{stage} {sec // 60}m{sec % 60:02d}s"
                          for stage, sec in s["timings"]) if s["timings"] else "not recorded"
        value = f"{s['currency']} {s['value']:,.2f}" if "value" in s else "—"
        rows.append(f"| {path.name} | {s['notes']:,} | {s['primary']:.0%} | "
                    f"{s.get('attempt', 'none computed')} | {s.get('flagged', '—')} | "
                    f"{s.get('removed', '—')} | {s.get('untraced', '—')} | {s.get('problems', '—')} | "
                    f"{s.get('rating', '—')} | {value} | {s['review'] or '—'} | {times} |")
    return "\n".join(rows)


def _revalue_source(replay: dict):
    """Return the selected pitch and its computed valuation without calling an LLM."""
    found = shipped(replay)
    if found is None:
        raise ValueError("the replay has no usable pitch/model to value")
    label, result = found
    model = result.headline.get("model")
    if not isinstance(model, dict):
        raise ValueError("the selected pitch has no model assumptions")
    return label, result, model


def write_assumptions_template(replay_path: Path, output_path: Path | None = None) -> Path:
    """Write the selected run's editable valuation inputs as a JSON workbench."""
    saved = load(replay_path)
    label, result, model = _revalue_source(saved)
    editable = copy.deepcopy(model)
    # Pin the effective rate into the template so changing it is explicit and does not
    # silently get replaced by the house CAPM rate during a custom valuation.
    editable["wacc"] = result.valuation.wacc
    payload = {
        "_instructions": (
            "Edit values under model, then run: python replay.py revalue <replay> "
            "<this-file>. Revenue/segment amounts are in reporting-currency millions; "
            "margins, tax, WACC and growth are decimal fractions (0.10 = 10%). "
            "Market price, shares, cash, debt and FX remain fixed to the saved run."
        ),
        "symbol": saved["symbol"],
        "based_on": replay_path.name,
        "selected_draft": label,
        "model": editable,
    }
    target = output_path or REPLAY_DIR / "assumptions" / f"{saved['symbol']}_assumptions.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


def _merge_model(base: dict, override: dict) -> dict:
    """Recursively replace only user-supplied model inputs; arrays replace as a whole."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_model(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def revalue(replay_path: Path, assumptions_path: Path) -> str:
    """Compare the saved pitch with user-edited assumptions using the saved market data."""
    saved = load(replay_path)
    try:
        payload = json.loads(assumptions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read assumptions file: {exc}") from exc
    return revalue_saved(saved, payload)


def revalue_saved(saved: dict, payload: dict) -> str:
    """Value edited assumptions against an already loaded saved replay."""
    import valuation

    _, result, base_model = _revalue_source(saved)
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), dict):
        raise ValueError("assumptions JSON must contain a `model` object")
    symbol = str(payload.get("symbol") or "").upper()
    if symbol and symbol != saved["symbol"].upper():
        raise ValueError(f"assumptions are for {symbol}, but replay is for {saved['symbol']}")
    custom_model = _merge_model(base_model, payload["model"])
    # An explicit WACC in the assumptions file is user-controlled. If omitted, the
    # original house WACC is retained for comparability.
    cost = None
    if custom_model.get("wacc") is None and config.HOUSE_WACC:
        cost = valuation.cost_of_capital(saved["data"], custom_model)
    import pitch as pitch_mod
    peers = saved.get("peers", {}).get(",".join(pitch_mod.peer_key(custom_model)), [])
    try:
        custom = valuation.compute(custom_model, saved["data"], peers, cost=cost)
    except valuation.ModelInputError as exc:
        raise ValueError(f"assumptions cannot be valued: {exc}") from exc
    custom_weighted = (custom.weighted_per_share if custom.weighted_per_share is not None
                       else custom.per_share)
    custom_upside = (custom.weighted_upside_pct if custom.weighted_upside_pct is not None
                     else custom.upside_pct)
    original = result.valuation
    terminal = (f"terminal growth {custom.growth:.1%}" if custom.growth is not None else
                f"exit multiple {custom.exit_multiple:.1f}x")
    lines = [f"{saved['symbol']} assumption comparison (saved market data; no network refresh)",
             f"Source replay: {saved.get('symbol', '')} {saved.get('saved_at', '')}".rstrip(),
             f"Original model: {original.currency} {original.recommendation_value:,.2f} "
             f"({original.recommendation_upside:+.1f}%, {original.rating})",
             f"Your assumptions: {custom.currency} {custom_weighted:,.2f} "
             f"({custom_upside:+.1f}%, {valuation.rating_for(custom_upside)})",
             f"Base DCF: {custom.currency} {custom.per_share:,.2f}; "
             f"WACC {custom.wacc:.1%}; {terminal}",
             f"Forecast revenue CAGR: {custom.model_cagr:.1%}" if custom.model_cagr is not None
             else "Forecast revenue CAGR: n.a."]
    if custom.warnings:
        lines += ["", "Valuation checks:", *[f"- {warning}" for warning in custom.warnings]]
    return "\n".join(lines)


def logging_setup() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(argv: list[str]) -> int:
    # The Windows console's code page cannot print the "→" the checks quote.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) >= 2 and argv[0] == "run":
        asyncio.run(_run(argv[1]))
        return 0
    if len(argv) >= 2 and argv[0] == "regather":
        asyncio.run(_regather(Path(argv[1])))
        return 0
    if argv and argv[0] == "score":
        print(score(live="--live" in argv))
        return 0
    if len(argv) >= 2 and argv[0] == "compare":
        print(compare([Path(a) for a in argv[1:]]))
        return 0
    if len(argv) >= 2 and argv[0] == "rewrite":
        asyncio.run(_rewrite(Path(argv[1])))
        return 0
    if len(argv) >= 2 and argv[0] == "assumptions":
        output = Path(argv[2]) if len(argv) >= 3 else None
        try:
            print(write_assumptions_template(Path(argv[1]), output))
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if len(argv) >= 3 and argv[0] == "revalue":
        try:
            print(revalue(Path(argv[1]), Path(argv[2])))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if argv and argv[0] == "check":
        files = [Path(a) for a in argv[1:]] or replay_files()
        for path in files:
            print(f"== {path.name}")
            for label, problems in check(load(path)):
                print(f"  {label}: {len(problems)} problem(s)")
                for p in problems:
                    print(f"    - {p}")
        return 0
    if argv and argv[0] == "ledger":
        totals, lines = ledger_report([Path(a) for a in argv[1:]] or replay_files())
        print("\n".join(lines))
        print("total: " + ", ".join(f"{n} {k}" for k, n in totals.items()))
        return 0
    if argv and argv[0] == "golden":
        diff = golden_diff(update="--update" in argv)
        for name, (lost, new) in diff.items():
            print(f"== {name}")
            for line in lost:
                print(f"  - no longer flagged: {line}")
            for line in new:
                print(f"  + newly flagged:     {line}")
        print("updated" if "--update" in argv else f"{len(diff)} replay(s) differ")
        return 1 if diff else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
