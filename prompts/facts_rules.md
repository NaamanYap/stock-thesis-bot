
# Figures: cite them by name, never type them

Every figure you print is one of these five, and nothing else:

- **A figure from the figure sheet** (in the message, before the research notes): its name
  in double braces, `{{ratings_revenue_growth_2Q26}}`. The code prints the value and its
  source, e.g. "16.6% (computed from earnings release (8-K exhibit 99), filed
  2026-07-28)". Write no unit, currency or source around it: the printed value has them.
  Each name says what the figure is and its period; use it for exactly that.
- **A share, ratio or change between two sheet figures** the sheet does not already give:
  `{{share:a/b}}` (a as a percentage of b), `{{ratio:a/b}}` (a over b, as a multiple),
  `{{change:a/b}}` (growth from a to b), with their names. Both must be the same unit and
  period basis.
- **A value of your model:** the placeholders listed above (`{{wacc}}`,
  `{{revenue:FY2030}}`, `{{ebit_margin:FY2030}}`, `{{segment_growth.Ratings:FY2028}}`,
  `{{downside_revenue_cut}}`, `{{implied_cagr}}`, `{{ev_ebitda}}`…). Every growth rate,
  margin, D&A, capex, FCF and terminal input you discuss has one.
- **A figure the company states in its own words** in a research-notes block marked
  PRIMARY (its release, filing or investor page) that the sheet does not hold: type it
  as the company wrote it, with the words it uses around it ("a contracted net sales
  run-rate of EUR 1.4bn", "management expects Optical and IP Networks to grow 18-20%").
  The code finds the company's sentence and prints the figure as written there, with its
  source. One it cannot find in the company's own sentences (a web page's, a peer's, one
  from memory, one reworded beyond recognition, or a bound like "more than $7 billion"
  written as "$7 billion") is deleted.
- **Your own estimate:** declare it in the JSON header under `assumptions`, then cite it
  as `{{assume:id}}`. It prints as "3-4% (my assumption)" and is listed in the document's
  Assumptions table with your basis. Each has one figure or a range. The code keeps an
  assumption only when it is valid:
  - **It is your judgement about the future,** or about something no filing reports (a
    price increase, a share of a market, a target leverage). A figure for a period already
    reported is a fact: cite it from the sheet, or leave it out.
  - **It is not on the sheet.** If it is, the code cites the sheet figure instead.
  - **It is not someone else's figure.** A basis may reason from sourced facts to a
    figure of your own ("order visibility runs to 2028 on management's 27% market CAGR",
    for a 3-4 year cycle). A basis that names a source (a filing, guidance, management,
    consensus, IMF, Bloomberg, Gartner…) and is only a citation, with no reasoning of its
    own, makes the value that source's, and it is refused; so is a value the research
    notes give. The company's own
    figure is written as above, not declared.
  - **Its basis gives a reason** in a sentence, not "estimate" or the number again.
  - **It is not one of your model's inputs.** Those have placeholders; an assumption that
    differs from them is refused.
  A refused assumption is sent back, and in the final draft its sentences are deleted.

Years, quarters and dates (FY2030, 2Q26, June 30, 2026) and names (S&P 500, 10-K) are not
figures. Write them as normal.

**Guidance is management's expectation:** say so ("management guides to
{{guidance_revenue_growth_FY2026}}"), or the code reads the sentence as a reported result.
**A figure for one period is not another's:** a sentence that names 4Q25 cannot cite a
2Q26 figure.

**What is not on the sheet or in the company's own words goes in words.** Market sizes,
market shares, competitors' and peers' figures, industry growth, funding rounds from web
pages and third parties: describe them ("a duopoly
with Moody's", "AI-native rivals are well funded"). The research notes hold them as context,
not as figures you can print. Don't estimate them from memory.

**The code checks every draft.** A typed figure, a name that is not on the sheet or in your
model, or an invalid assumption sends the draft back. If a typed figure still cannot be
matched, it stays in the report with a † for verification; lack of an automatic match is not
proof that it is wrong. Cite it where the research notes support it.
