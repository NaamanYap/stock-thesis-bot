"""Render a generated pitch to a PDF that reads like a research note."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

import charts
import config
import history
from market_data import MarketData
import pitch as pitch_mod
from pitch import Pitch, street_view
import valuation
from valuation import pct

INK = colors.HexColor("#000000")
MUTED = colors.HexColor("#5b6570")
RULE = colors.HexColor("#d6dbe0")
BAND = colors.HexColor("#d9d9d9")
LINK = "#1f4fbf"
ACCENT = colors.HexColor("#1a5c3a")

RATING_COLOR = {
    "BUY": colors.HexColor("#1a5c3a"),
    "HOLD": colors.HexColor("#8a6d1f"),
    "SELL": colors.HexColor("#9b2c2c"),
}

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
CODE_RE = re.compile(r"`([^`]+?)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
# The model opens the body with a header line that the cover block already carries.
HEADER_LINE_RE = re.compile(r"^.{0,120}\|\s*Current Price:.*?Recommendation:.*?$",
                            re.IGNORECASE | re.MULTILINE)
PLACEHOLDER_NAME_RE = re.compile(r"\{\{([^{}]+)\}\}")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*(?:\}\}|$)|\}\}")
CLAIM_MARKER_RE = re.compile(r"\[#([A-Za-z0-9_-]+)\]")
PROSE_CELL = 40
NUMERIC_CELL_RE = re.compile(r"^[(+\-]?[A-Z]{0,3}\s?[\d.,]+\s?(%|x|bn|mn|pp|pts?|days)?\)?"
                             r"(\s*\([^)]*\))?$|^(n\.?a\.?|n\.m\.|-)$")
# The model's own labels after each point; the memo format reads as argument without them.
LABEL_TAIL_RE = re.compile(r"\s*\*?\((?:Fact|Calculation|Inference|Assumption|Hypothesis|"
                           r"Claim)\s+[A-Za-z0-9_-]+,\s*\w+ confidence\)\*?")

_PDF_TRANSLATION = str.maketrans({
    "\u00a0": " ", "\u00ad": "", "\u200b": "",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2212": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2022": "-",
    # Helvetica/WinAnsi cannot render the dagger that marks unmatched figures.
    # Preserve that warning in the PDF as searchable plain text.
    "\u2020": " [unverified]",
    # A replacement character means an upstream decoder lost the original glyph. Render
    # it as punctuation instead of dropping it silently in the searchable PDF text.
    "\ufffd": "--",
    "\u00d7": "x", "\u00b1": "+/-", "\u2264": "<=", "\u2265": ">=",
    "\u2248": "~", "\u2192": "->", "\u2194": "<->", "\u00b0": " deg",
    "\u20ac": "EUR ", "\u00a3": "GBP ", "\u00a5": "JPY ",
    "\u20b9": "INR ", "\u20a9": "KRW ", "\u00ae": "(R)",
    "\u2122": " TM", "\u03b2": "beta", "\u0394": "Delta",
})


def _pdf_safe(text: str) -> str:
    """Normalize model typography to characters supported by ReportLab's Helvetica.

    The built-in PDF fonts use WinAnsi; non-breaking hyphens, smart punctuation and a
    few common symbols were silently dropped in delivered reports (for example,
    "pre-provision" became "preprovision"). Transliterate the remaining Unicode so
    text remains searchable and no replacement glyphs appear in the PDF.
    """
    text = unicodedata.normalize("NFKD", str(text).translate(_PDF_TRANSLATION))
    return text.encode("ascii", "ignore").decode("ascii")


def _strip_header_line(body: str) -> str:
    """Drop the redundant one-line header so the cover block is the only banner."""
    stripped = body.lstrip()
    first, sep, rest = stripped.partition("\n")
    if HEADER_LINE_RE.match(first.strip()):
        return rest.lstrip()
    return stripped


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=base["BodyText"], fontName="Times-Roman", fontSize=10,
        leading=12.6, textColor=INK, spaceAfter=6, alignment=TA_JUSTIFY,
    )
    styles = {
        "body": body,
        "h1": ParagraphStyle("H1", parent=body, fontName="Times-Bold", fontSize=12,
                             leading=15, spaceBefore=10, spaceAfter=5, alignment=0),
        "h2": ParagraphStyle("H2", parent=body, fontName="Times-Bold", fontSize=11,
                             leading=14, spaceBefore=10, spaceAfter=5, alignment=0),
        "h3": ParagraphStyle("H3", parent=body, fontName="Times-Roman", fontSize=10,
                             leading=12.6, spaceBefore=6, spaceAfter=3, alignment=0),
        "caption": ParagraphStyle("Caption", parent=body, fontName="Times-Roman",
                                  fontSize=10, leading=13, spaceBefore=8, spaceAfter=4,
                                  alignment=0),
        "header": ParagraphStyle("Header", parent=body, fontName="Times-Bold", fontSize=11,
                                 leading=14, spaceAfter=8, alignment=1),
        "bullet": ParagraphStyle("Bullet", parent=body, spaceAfter=3),
        "sub": ParagraphStyle("Sub", parent=body, fontName="Times-Bold", fontSize=10,
                              leading=13, spaceBefore=8, spaceAfter=3, alignment=0),
        "lead": ParagraphStyle("Lead", parent=body, fontSize=10.5, leading=15.5,
                               textColor=INK, spaceAfter=9),
        "meta": ParagraphStyle("Meta", parent=body, fontSize=9, leading=11.5,
                               textColor=INK),
        "title": ParagraphStyle("Title", parent=body, fontName="Helvetica-Bold", fontSize=19,
                                leading=23, spaceAfter=3, alignment=0),
        "cell": ParagraphStyle("Cell", parent=body, fontName="Helvetica", fontSize=7,
                               leading=9, spaceAfter=0, alignment=0),
        "cellnum": ParagraphStyle("CellNum", parent=body, fontName="Helvetica", fontSize=7,
                                  leading=9, spaceAfter=0, alignment=2),
        "cellhead": ParagraphStyle("CellHead", parent=body, fontName="Helvetica-Bold",
                                   fontSize=7, leading=9, spaceAfter=0, alignment=0),
        "cellheadnum": ParagraphStyle("CellHeadNum", parent=body, fontName="Helvetica-Bold",
                                      fontSize=7, leading=9, spaceAfter=0, alignment=2),
        "small": ParagraphStyle("Small", parent=body, fontSize=8, leading=10,
                                textColor=INK, alignment=0),
    }
    for name in ("h1", "h2", "h3", "caption", "sub"):
        styles[name].keepWithNext = 1
    return styles


def _inline(text: str) -> str:
    """Markdown inline syntax to reportlab's mini-HTML, XML-escaped first."""
    text = PLACEHOLDER_RE.sub("[value unavailable]", _pdf_safe(text))
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = LINK_RE.sub(r'<link href="\2" color="%s"><u>\1</u></link>' % LINK, out)
    out = BOLD_RE.sub(r"<b>\1</b>", out)
    out = ITALIC_RE.sub(r"<i>\1</i>", out)
    out = CODE_RE.sub(r'<font face="Courier" size="8.5">\1</font>', out)
    return out


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(rows: list[list[str]], st: dict, width: float) -> Table:
    header, *body_rows = rows
    ncols = max(len(r) for r in rows)
    header += [""] * (ncols - len(header))

    # Label and unit columns read left; figures sit right, as in a model printout.
    # A sentence in a figures column (the scenarios' "What happens") reads left and does
    # not decide the column's alignment.
    def prose(c: str) -> bool:
        return len(c) > PROSE_CELL and not NUMERIC_CELL_RE.match(c.strip("*"))

    def numeric(col: int) -> bool:
        cells = [r[col] for r in body_rows
                 if col < len(r) and r[col].strip() and not prose(r[col])]
        return col > 0 and bool(cells) and all(NUMERIC_CELL_RE.match(c.strip("*")) for c in cells)
    right = [numeric(c) for c in range(ncols)]

    # A "<< note" cell is the model's comment on the figure beside it, set off as the
    # house report does: "|<< as of 9 Oct 2026".
    def note(c: str) -> bool:
        return c.strip().startswith("<<")
    notes = [any(note(r[c]) for r in body_rows if c < len(r)) for c in range(ncols)]

    def cell(c: str, j: int, head: bool = False) -> Paragraph:
        if not head and note(c):
            return Paragraph(f'<font color="#5b6570">|&lt;&lt; '
                             f'{_inline(c.strip()[2:].strip())}</font>', st["cell"])
        style = ("cellhead" if head else "cell") + ("num" if right[j] and not prose(c) else "")
        return Paragraph(_inline(c), st[style])

    data = [[cell(c, j, True) for j, c in enumerate(header)]]
    for row in body_rows:
        row = row + [""] * (ncols - len(row))
        data.append([cell(c, j) for j, c in enumerate(row)])

    # First column carries labels and needs the room, a unit column little, a note column
    # the rest; the figures share what is left.
    unit = ncols > 2 and header[1].strip().lower() in ("unit", "")
    first = min(width * 0.34, max(width / ncols, width * 0.2)) if ncols > 2 else width / ncols
    if ncols > 6:
        first = width * (0.20 if ncols > 8 else 0.24)
    widths = [first] + [None] * (ncols - 1)
    if unit:
        widths[1] = width * 0.065
    if any(notes):
        j = notes.index(True)
        widths[j] = width * 0.42
    free = [j for j, w in enumerate(widths) if w is None]
    left = width - sum(w for w in widths if w is not None)
    for j in free:
        widths[j] = left / len(free)

    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 if ncols > 6 else 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 if ncols > 6 else 4),
    ]))
    return table


def _flow_list(items: list[str], st: dict, ordered: bool) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(_inline(i), st["bullet"]), leftIndent=13) for i in items],
        bulletType="1" if ordered else "bullet",
        bulletFontSize=10 if ordered else 9,
        bulletFontName="Times-Roman", start="1" if ordered else "•",
        leftIndent=14, bulletOffsetY=0 if ordered else -1, spaceAfter=7,
    )


def markdown_to_flowables(md: str, st: dict, width: float,
                          drawn: dict | None = None) -> list:
    """Convert the model's markdown subset into Platypus flowables.

    `drawn` maps chart names to drawings; a `<!-- chart:name -->` line places one. A
    marker with no drawing (no data for it) leaves nothing behind.
    """
    md = _pdf_safe(md)
    flow: list = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    pending: list[str] = []
    in_outputs = False

    def flush_para():
        nonlocal pending
        if pending:
            flow.append(Paragraph(_inline(" ".join(pending)), st["body"]))
            pending = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        chart = charts.MARKER.match(stripped)
        if chart:
            flush_para()
            if drawn and chart.group(1) in drawn:
                flow += [Spacer(1, 4), drawn[chart.group(1)], Spacer(1, 8)]
            i += 1
            continue

        # Pipe table: a header row followed by a separator row.
        if stripped.startswith("|") and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            flow.append(Spacer(1, 3))
            flow.append(_table(rows, st, width))
            flow.append(Spacer(1, 9))
            continue

        if stripped.startswith("#"):
            flush_para()
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            if level == 2:
                in_outputs = text.lower() == "model outputs"
                if in_outputs and flow:
                    flow.append(PageBreak())
            if level == 3 and in_outputs:
                # The code numbers Model Outputs' sections itself ("1a.", "2."), as the
                # house report does.
                flow.append(Paragraph(_inline(text), st["sub"]))
            elif level >= 4 and in_outputs:
                flow.append(Paragraph(_inline(text), st["caption"]))
            else:
                style = st["h1"] if level == 1 else st["h2"] if level == 2 else st["h3"]
                flow.append(Paragraph(f"<u>{_inline(text)}</u>", style))
            i += 1
            continue

        if BULLET_RE.match(line) or ORDERED_RE.match(line):
            flush_para()
            ordered = bool(ORDERED_RE.match(line))
            pattern = ORDERED_RE if ordered else BULLET_RE
            items: list[str] = []
            while i < len(lines) and pattern.match(lines[i]):
                items.append(pattern.match(lines[i]).group(1).strip())
                i += 1
                # Fold hanging continuation lines into the current item.
                while (i < len(lines) and lines[i].startswith(("  ", "\t"))
                       and lines[i].strip() and not pattern.match(lines[i])):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            flow.append(_flow_list(items, st, ordered))
            continue

        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            flush_para()
            flow.append(HRFlowable(width="100%", thickness=0.5, color=RULE,
                                   spaceBefore=4, spaceAfter=8))
            i += 1
            continue

        pending.append(stripped)
        i += 1

    flush_para()
    return flow


def _street_block(pitch: Pitch, data: MarketData, st: dict, width: float) -> list:
    """The Street's targets beside this pitch's value, so every call is read against them."""
    rows = street_view(data)
    if not rows:
        return [Paragraph("Street targets: not available from the data source.", st["meta"])]
    intrinsic = pitch.get("intrinsic_price")
    if intrinsic:
        up = f" ({pct(pitch.get('upside_pct'))})" if isinstance(pitch.get("upside_pct"), (int, float)) else ""
        rows = [("This pitch", f"{data.currency} {intrinsic:,.2f}{up}, {pitch.get('recommendation', '')}")] + rows
    cells = [[Paragraph(f"<b>{_inline(who)}</b>", st["small"]), Paragraph(_inline(what), st["small"])]
             for who, what in rows]
    table = Table(cells, colWidths=[width * 0.25, width * 0.75], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("LINEBELOW", (0, 0), (-1, 0), 0.4, RULE),
    ]))
    return [table]


def _cover(pitch: Pitch, data: MarketData, st: dict, width: float) -> tuple[list, list]:
    """The one-line memo header for the top, and everything else the cover used to carry
    as notes for the end, under "Review notes and sources"."""
    rating = str(pitch.get("recommendation", "")).upper()
    currency = pitch.get("currency") or data.currency
    upside = pitch.get("upside_pct")
    current = pitch.get("current_price") or data.price
    intrinsic = pitch.get("intrinsic_price")

    name = _inline(pitch.get("company") or data.name)
    exchange = data.exchange or pitch.get("exchange", "")
    ticker = pitch.get("ticker") or data.symbol
    now = f"{currency} {current:,.2f}" if current else "n.a."
    # The rating is set on the probability-weighted value when there are scenarios.
    value = f"{currency} {intrinsic:,.2f}" if intrinsic else "n.a."
    if isinstance(upside, (int, float)):
        value += f" ({pct(upside)})"
    head = [Paragraph(
        f"{name} ({_inline(exchange)}: {_inline(ticker)}) | Current Price: {now} |<br/>"
        f"Intrinsic Price: {value} | Recommendation: {rating or 'n.a.'}", st["header"])]

    # The equity route's Model Outputs stop at 6 (no returns-on-capital section).
    number = (f"{valuation.REVIEW_SECTION - (pitch.valuation.family == 'equity')}. "
              if pitch.valuation is not None else "")
    flow = [Paragraph(f"{number}Review notes and sources", st["sub"]),
            Paragraph(f"<b>Report date:</b> {date.today().strftime('%d %B %Y')}.", st["meta"])]
    flow += _street_block(pitch, data, st, width)
    v = pitch.valuation
    if v is not None and v.implied_cagr is not None and v.model_cagr is not None:
        street = (f", the Street's target {v.street_cagr:.1%}" if v.street_cagr is not None
                  else "")
        flow.append(Paragraph(
            f"<b>What the price assumes:</b> revenue growth of {v.implied_cagr:.1%} a year to "
            f"{v.rows[-1].label} at this model's margins and discount rate{street}; the model "
            f"forecasts {v.model_cagr:.1%}.", st["meta"]))
    if pitch.history:
        lines = history.describe(pitch.history, pitch.get("intrinsic_price"))
        runs, gap = lines[:len(pitch.history)], lines[len(pitch.history):]
        flow.append(Paragraph("<b>Earlier runs of this ticker:</b> "
                              + "; ".join(_inline(l) for l in runs) + "."
                              + "".join(f" {_inline(g)}." for g in gap), st["meta"]))
    if pitch.get("one_liner"):
        flow.append(Paragraph(f"<b>In one line:</b> {_inline(pitch.get('one_liner'))}",
                              st["meta"]))

    conviction = pitch.get("conviction")
    meta = [f"Conviction: <b>{conviction}</b>"] if conviction else []
    if pitch.writer:
        backup = " (backup model)" if pitch.writer != config.MODEL else ""
        meta.append(f"Written by: <b>{_inline(pitch.writer)}</b>{backup}")
    if meta:
        flow.append(Paragraph(" &nbsp;|&nbsp; ".join(meta), st["meta"]))

    # Problems the write turn did not fix. A pitch that still contradicts its own model
    # says so here, where the reader looks first, rather than passing as clean.
    repaired = []
    if pitch.removed:
        n = len(pitch.removed)
        repaired.append(f"{n} sentence{'s' if n != 1 else ''} with a figure that contradicted "
                        "the model or did not add up "
                        f"{'were' if n != 1 else 'was'} removed (listed at the end)")
    if pitch.unverified:
        n = pitch.unverified
        repaired.append(f"{n} figure{'s' if n != 1 else ''} marked [unverified] lack a matched citation "
                        "and remain visible for source verification")
    if repaired:
        flow.append(Paragraph("<b>Checked by code:</b> " + "; ".join(repaired) + ".",
                              st["meta"]))
    quality = []
    if pitch.source_check_available:
        quality.append(f"structured data: {len(pitch.source_conflicts)} conflict(s), "
                       f"{len(pitch.source_exclusions)} excluded figure(s), "
                       f"{len(pitch.source_gaps)} research gap(s)")
    if pitch.data_reconciliations:
        n = sum(1 for line in pitch.data_reconciliations.splitlines()
                if line.startswith("| ") and not line.startswith(("| ---", "| Line item")))
        quality.append(f"{n} annual figure(s) reconciled to company filings")
    if pitch.review_status != "not run":
        quality.append(f"review: {pitch.review_status}")
    if quality:
        flow.append(Paragraph("<b>What the checks found:</b> " + "; ".join(_inline(x)
                              for x in quality) + ".", st["meta"]))
    if pitch.review_log:
        flow.append(Paragraph("<b>Review rounds:</b> " + " ".join(
            _inline(_round_text(r)) for r in pitch.review_log), st["meta"]))
    if pitch.withheld:
        flow.append(Paragraph(f"<b>Rating withheld:</b> {_inline(pitch.withheld)}.",
                              st["meta"]))
    model = pitch.get("model") if isinstance(pitch.get("model"), dict) else {}
    revisions = model.get("method_revisions") if isinstance(
        model.get("method_revisions"), list) else []
    flow.append(Paragraph("<b>Method revisions:</b> " + (
        f"{len(revisions)}, listed in Model Outputs." if revisions else "none."), st["meta"]))
    if v is not None and v.method_choices:
        flow.append(Paragraph("<b>Method choices:</b> " + " ".join(
            f"<i>{_inline(name)}:</i> {_inline(text.rstrip('.'))}." for name, text in
            v.method_choices), st["meta"]))
    open_review = pitch.review if pitch.review and not pitch.review_addressed else []
    unresolved = ([f"{len(pitch.checks)} code check(s)"] if pitch.checks else []) + (
        [f"{len(open_review)} reviewer point(s)"] if open_review else [])
    flow.append(Paragraph("<b>Left unresolved:</b> " + (" and ".join(unresolved)
                          + ", listed below." if unresolved else "none."), st["meta"]))
    return head, flow


def _round_text(r: dict) -> str:
    """One review round for the reader: "Round 2 (draft 2): 1 FAIL, 2 PARTIAL
    (THIN_FORWARD_VIEW x2, PERIOD_ERROR); 3 code checks; full rewrite."""
    found = (f"{r['fail']} FAIL, {r['partial']} PARTIAL ("
             + ", ".join(f"{c} x{n}" if n > 1 else c for c, n in r["codes"].items()) + ")"
             if r["codes"] else "clean")
    bits = [found]
    if r.get("code_checks"):
        bits.append(f"{r['code_checks']} code check(s)")
    if r.get("rating_change"):
        bits.append(f"rating moved {r['rating_change']}")
    bits.append(r.get("repair") or "no repair")
    return f"Round {r['round']} (draft {r['draft']}): " + "; ".join(bits) + "."


def _check_text(text: str) -> str:
    """A check's message for the reader: the placeholder it names, as a name."""
    return _inline(PLACEHOLDER_NAME_RE.sub(lambda m: f"`{m.group(1).strip()}`", text))


def _open_checks(pitch: Pitch, st: dict) -> list:
    """The problems the code checks found and the writer did not fix."""
    if not pitch.checks:
        return []
    return [KeepTogether([
                Paragraph("Unresolved checks", st["h2"]),
                HRFlowable(width="100%", thickness=0.5, color=RULE, spaceBefore=1, spaceAfter=7),
                Paragraph("The code checks found these in the delivered draft and the writer "
                          "did not resolve them. Names in this typeface are the model's "
                          "placeholders.", st["body"])]),
            ListFlowable([ListItem(Paragraph(_check_text(c), st["small"]), leftIndent=13)
                          for c in pitch.checks],
                         bulletType="bullet", bulletFontSize=7, leftIndent=14, spaceAfter=7)]


def _reviewer_notes(pitch: Pitch, st: dict) -> list:
    """High-severity issues a reviewing model raised that the revision did not close.

    With none open, one line still says whether the review passed or never ran: the
    LHX, NFLX and ANF pitches of 2026-09-24/25 left no way to tell.
    """
    if not pitch.review:
        if pitch.review_status == "not run":
            return []
        return [Paragraph(f"<i>Second-model review: {_inline(pitch.review_status)}.</i>",
                          st["body"])]
    said = ("reviewed the first draft of this pitch and raised these points. The delivered "
            "draft was written to address them and was not reviewed again, so check whether "
            "it does" if pitch.review_addressed else
            f"reviewed this pitch before delivery ({pitch.review_status}) and left these "
            "points open")
    flow = [Paragraph("Reviewer notes", st["h2"]),
            HRFlowable(width="100%", thickness=0.5, color=RULE, spaceBefore=1, spaceAfter=7),
            Paragraph(f"A second AI model {said}. They are its opinion, not verified "
                      "findings: an objection that a figure is wrong is shown only when the "
                      "passage it quotes is in the research notes.",
                      st["body"])]
    items = []
    for issue in pitch.review:
        where = f"<b>{_inline(issue['section'])}.</b> " if issue.get("section") else ""
        claim = f"<i>{_inline(issue['claim'])}</i>: " if issue.get("claim") else ""
        items.append(where + claim + _inline(issue["problem"]))
    flow.append(ListFlowable(
        [ListItem(Paragraph(i, st["bullet"]), leftIndent=13) for i in items],
        bulletType="bullet", bulletFontSize=7, leftIndent=14, spaceAfter=7))
    return flow


def _claim_labels(outline: dict) -> dict[str, str]:
    labels = {}
    for section in outline.get("outline", []) if isinstance(outline, dict) else []:
        if not isinstance(section, dict):
            continue
        for point in section.get("points", []):
            if not isinstance(point, dict) or not point.get("id"):
                continue
            kind = str(point.get("kind") or "claim").strip().title()
            confidence = str(point.get("confidence") or "unrated").strip().lower()
            labels[str(point["id"]).strip()] = f"({kind} {point['id']}, {confidence} confidence)"
    return labels


def _strip_claim_markers(body: str) -> str:
    """Drop claim markers and the writer's own claim labels from the memo text."""
    return LABEL_TAIL_RE.sub("", CLAIM_MARKER_RE.sub("", body))


def _source_conflicts(pitch: Pitch, st: dict, width: float) -> list:
    """Section 8's source conflicts: filed figures that replaced the aggregator's, and the
    conflicts the structured fact check found."""
    flow = []
    if pitch.data_reconciliations or pitch.source_conflicts:
        flow.append(Paragraph("Source conflicts", st["h2"]))
        if pitch.data_reconciliations:
            flow.append(Paragraph("Annual aggregator figures that differed from company filings "
                                  "were replaced with the filed figures used in the model.", st["body"]))
            flow += markdown_to_flowables(pitch.data_reconciliations, st, width)
        if pitch.source_conflicts:
            flow.append(_flow_list(pitch.source_conflicts[:30], st, False))
    elif pitch.source_check_available:
        flow.append(Paragraph("<b>Source conflicts:</b> none; the structured fact check found "
                              "no unresolved conflicts.", st["meta"]))
    else:
        flow.append(Paragraph("<b>Source conflicts:</b> n.a.", st["meta"]))
    return flow


def _sources_cited(pitch: Pitch, st: dict) -> list:
    """Section 8's last lines: what was left out (listed after the section), then the
    sources."""
    left_out = ([f"{len(pitch.source_exclusions)} excluded figure(s)"]
                if pitch.source_exclusions else []) + (
        [f"{len(pitch.source_gaps)} research gap(s)"] if pitch.source_gaps else [])
    flow = [Paragraph("<b>Left out:</b> " + (" and ".join(left_out) + ", listed below."
                                             if left_out else "none."), st["meta"])]
    sources = []
    seen = set()
    for source in pitch.sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        title = str(source.get("title") or url).strip()
        if not title or (url and url in seen):
            continue
        if url:
            seen.add(url)
        sources.append((title, url))
    if not sources:
        return flow + [Paragraph("<b>Sources cited:</b> n.a.", st["meta"])]
    flow += [Paragraph("Sources cited", st["h2"]),
             Paragraph("Pages and filings read for this pitch. Sources cited in the body "
                      "are identified alongside the figures they support.", st["small"])]
    omitted = max(len(sources) - 60, 0)
    for i, (title, url) in enumerate(sources[:60], 1):
        label = f"[{title}]({url})" if url else title
        flow.append(Paragraph(f"{i}. {_inline(label)}", st["small"]))
    if omitted:
        flow.append(Paragraph(f"{omitted} additional pages were read during research.", st["small"]))
    return flow


def _research_gaps(pitch: Pitch, st: dict) -> list:
    if not (pitch.source_exclusions or pitch.source_gaps):
        return []
    entries = ([f"Excluded: {x}" for x in pitch.source_exclusions[:20]]
               + [f"Not available: {x}" for x in pitch.source_gaps[:20]
                  # a gap that only repeats an exclusion's reason is listed once
                  if not any(x.split(": ", 1)[-1] in e for e in pitch.source_exclusions)])
    return [Paragraph("Research gaps and excluded figures", st["h2"]),
            _flow_list(entries, st, False)]


def _repair_notes(pitch: Pitch, st: dict, data: MarketData | None = None) -> list:
    """What the code took out of the text, and what the unverified label means."""
    flow = []
    if pitch.removed:
        flow += [Paragraph("Removed by the checks", st["h2"]),
                 HRFlowable(width="100%", thickness=0.5, color=RULE, spaceBefore=1,
                            spaceAfter=7),
                 Paragraph("These sentences were deleted from the text above because a figure in "
                           "them contradicted the computed model, a worked figure did not "
                           "come out, or the writer typed a figure instead of citing it from "
                           "the research notes:", st["body"])]
        # The model's figures filled in, any other name shown as one: SPOT's of 2026-10-07
        # listed its removed sentences with "[value unavailable]" in every placeholder.
        values = (pitch_mod._placeholder_values(pitch.valuation, data)
                  if pitch.valuation is not None and data is not None else {})

        def shown(text: str) -> str:
            filled = PLACEHOLDER_NAME_RE.sub(
                lambda m: (pitch_mod._fill_placeholders(m.group(0), values)[0]
                           if not pitch_mod._fill_placeholders(m.group(0), values)[1]
                           else m.group(0)), text)
            return _check_text(filled)
        flow.append(ListFlowable(
            [ListItem(Paragraph(f"<i>{shown(sentence)}</i> ({shown(why)})", st["bullet"]),
                      leftIndent=13) for sentence, why in pitch.removed],
            bulletType="bullet", bulletFontSize=7, leftIndent=14, spaceAfter=7))
    if pitch.unverified:
        flow.append(Paragraph("[unverified] No citation matched automatically. The figure remains in the "
                              "text and may be accurate; verify it against the cited source.",
                              st["small"]))
    return flow


def build(pitch: Pitch, data: MarketData, out_dir: Path | None = None) -> Path:
    """Write the pitch PDF and return its path."""
    out_dir = out_dir or config.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{data.symbol}_pitch_{date.today().isoformat()}.pdf"

    st = _styles()
    margin = 17 * mm
    width = A4[0] - 2 * margin

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=margin, rightMargin=margin, topMargin=16 * mm, bottomMargin=18 * mm,
        title=f"{data.name} — Investment Pitch", author="Stock Thesis Bot",
        subject=f"{data.symbol} investment thesis",
    )

    story, notes = _cover(pitch, data, st, width)
    body = _strip_claim_markers(_strip_header_line(pitch.body))
    story += markdown_to_flowables(body, st, width,
                                   charts.build(pitch.valuation, data, width))
    # The checks, the review and the sources follow as notes, a level below the memo.
    sub = dict(st, h2=st["sub"])
    story += notes
    story += _source_conflicts(pitch, sub, width)
    story += _sources_cited(pitch, sub)
    story += _open_checks(pitch, sub)
    story += _reviewer_notes(pitch, sub)
    story += _repair_notes(pitch, sub, data)
    story += _research_gaps(pitch, sub)

    def furniture(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Times-Roman", 8)
        canvas.setFillColor(INK)
        canvas.drawCentredString(A4[0] / 2, 11 * mm, str(doc_.page))
        canvas.restoreState()

    story += [
        Spacer(1, 14),
        HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=6),
        Paragraph(config.DISCLAIMER, st["small"]),
    ]

    doc.build(story, onFirstPage=furniture, onLaterPages=furniture)
    return path
