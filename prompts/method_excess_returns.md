# This company is valued on returns to equity, not by enterprise DCF

A bank or insurer borrows to lend and invest: deposits, policy reserves and funding are its raw
material, not its capital structure. So there is no enterprise value, no WACC and no bridge.
Build the model the way a bank or insurance analyst does: forecast the operating drivers, let
code build net income from them, roll book value forward with what is retained, and value the
equity as **book value today plus the present value of the returns above the cost of equity**
(the excess-return, or residual-income, model). Code also runs a dividend discount on the same
inputs as an identity check, and shows the price to book, price to tangible book and P/E the
value implies.

**This replaces the FCFF fields of the `model` object.** Do not give `revenue`, `segments`,
`ebit_margin`, `gross_margin`, `da_pct_revenue`, `capex_pct_revenue`, `nwc_pct_revenue_change`,
`captive_finance_debt` or an exit multiple; they are not read. Everything else about the header
(`years`, `base_year`, `peers`, `drivers`, `thresholds`, `material_events`, scenario
probabilities and rationales, `view`) is as described above.

## The fields

All money in **millions of the reporting currency**; rates as fractions (0.025 for 2.5%). A
single number instead of a list applies to every year.

For a **bank** (and any lender, card issuer or broker-dealer), `model.bank`:

- `earning_assets`: loans, securities and interbank assets that earn interest, at each year
  end. Net interest income is earned on the average of the year's opening and closing, so give
  `model.base_earning_assets` (the base year's, from the filing) for the first year's average.
- `nim`: net interest margin on average earning assets. Start from the reported NIM; argue
  every move from rates, deposit beta, funding mix and asset repricing.
- `non_interest_income`: fees, trading, wealth and card income, in money each year.
- `efficiency_ratio`: operating expenses over net revenue (NII plus non-interest income).
- `loans`: gross loans at each year end; `credit_cost`: provisions over loans. Normalise it
  through the cycle: a year of benign credit is not the run rate.

For an **insurer**, `model.insurer`:

- `net_premiums`: net premiums earned; `combined_ratio`: losses and expenses over premiums
  (below 1.0 is an underwriting profit); `invested_assets` at each year end, with
  `model.base_invested_assets`; `investment_yield` on average invested assets; and
  `other_income`: the **pre-tax profit** (not revenue) of businesses outside underwriting and
  investment, such as fee or service segments, if material; code checks it against total profit.

For both:

- `tax_rate`, and `payout`: dividends plus net buybacks as a share of net income to common.
  Payout is capital management, not a free choice: a bank pays out what its capital ratio
  allows. Give **`model.capital`** for a bank: `cet1_ratio`, `rwa` (risk-weighted assets,
  reporting mn) and `requirement` (its CET1 requirement including buffers), with `source`, all
  from the latest filing. Code rolls CET1 forward and sends the model back if a payout takes
  the bank below its requirement.
- `preferred_dividends` (reporting mn a year) only if the market data's figure is wrong.
- `terminal`: `roe`, the return on equity the business sustains for ever after the forecast,
  and `growth` (below the risk-free rate). Code computes the payout that growth leaves
  (1 - g / ROE). A terminal ROE above the cost of equity is a franchise that keeps a spread
  for ever: argue it in `terminal.rationale` from the deposit franchise, scale or underwriting
  record, and keep it near the ROE your final forecast year earns.
- `roe_rationale` when your ROE rises above the best the company has reported, and
  `first_year_rationale` when the first year's net interest income or net income moves far
  from the last reported year.
- `bull` / `bear`: change any of `nim`, `efficiency_ratio`, `credit_cost`, `combined_ratio`,
  `investment_yield`, `payout`, `terminal` (its `roe`, `growth`), `earning_assets_change` or
  `net_premiums_change` (a level difference from the base case, e.g. -0.05), and
  `cost_of_equity`, each with a `rationale`. A credit cycle is the bear case most banks need:
  credit cost at its last-downturn level for two years.

Book value today is read from the balance sheet (common equity, preferred excluded); do not
type it.

## The discount rate

The code discounts at the **cost of equity** (CAPM: risk-free rate plus beta times the equity
risk premium), stated in the message. `{{wacc}}` and `{{cost_of_equity}}` both cite it. A
`wacc_premium` adds to it, under the same rules as above.

## Placeholders

Cite, never type: `{{intrinsic}}`, `{{upside}}`, `{{rating}}`, `{{cost_of_equity}}`, `{{g}}`,
`{{terminal_roe}}`, `{{book_value}}`, `{{book_per_share}}`, `{{tangible_book_per_share}}`,
`{{pb}}` (today's price to book), `{{implied_pb}}`, `{{ptbv}}`, `{{implied_ptbv}}`,
`{{implied_roe}}` (the sustainable ROE today's price assumes), `{{ddm_value}}`; per forecast
year `{{net_income:FY2027}}`, `{{roe:FY2027}}`, `{{rote:FY2027}}`, `{{nim:FY2027}}`,
`{{nii:FY2027}}`, `{{efficiency_ratio:FY2027}}`, `{{credit_cost:FY2027}}`,
`{{provisions:FY2027}}`, `{{combined_ratio:FY2027}}`, `{{payout:FY2027}}`,
`{{book_value:FY2027}}`, `{{cet1_ratio:FY2027}}`; and the scenario values as above. Reported
ROE by year is `{{roe:FY2025}}`.

## Example `model`

```json
"model": {
  "base_year": "FY2025",
  "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
  "base_earning_assets": 3300000,
  "bank": {
    "earning_assets": [3420000, 3540000, 3660000, 3780000, 3900000],
    "nim": [0.0270, 0.0265, 0.0262, 0.0260, 0.0260],
    "non_interest_income": [95000, 98000, 101000, 104000, 107000],
    "efficiency_ratio": [0.54, 0.54, 0.53, 0.53, 0.53],
    "loans": [1400000, 1450000, 1500000, 1550000, 1600000],
    "credit_cost": [0.0075, 0.0080, 0.0080, 0.0080, 0.0080]
  },
  "tax_rate": 0.23,
  "payout": [0.70, 0.70, 0.65, 0.65, 0.65],
  "capital": {"cet1_ratio": 0.155, "rwa": 1800000, "requirement": 0.123,
              "source": "2Q26 10-Q, regulatory capital table"},
  "terminal": {"roe": 0.15, "growth": 0.03,
               "rationale": "the deposit franchise keeps funding costs below peers through the cycle, so returns stay above the cost of equity"},
  "peers": ["BAC", "WFC", "C"],
  "bear": {"credit_cost": [0.012, 0.014, 0.010, 0.009, 0.008], "nim": 0.0245,
           "rationale": "a recession doubles charge-offs for two years while rate cuts compress the margin"},
  "bull": {"nim": 0.0285, "efficiency_ratio": 0.51, "terminal": {"roe": 0.17},
           "rationale": "higher-for-longer rates hold asset yields while technology spend lowers the cost base"}
}
```
