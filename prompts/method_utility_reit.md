# This company is valued on returns to equity, not by enterprise DCF

A regulated utility earns what its regulator allows on the equity share of its rate base, and
grows by investing in that rate base with new debt and equity. A REIT pays out most of its cash
and grows by buying and building with new debt and equity. An enterprise DCF charges these
businesses for decades of growth capex and credits little of the return the regulator or the
tenants pay on it. Build the model the way a utility or REIT analyst does, from the operating
drivers; code values what is left for shareholders at the cost of equity, checked against a
dividend discount on the same inputs.

**This replaces the FCFF fields of the `model` object.** Do not give `revenue`, `segments`,
`ebit_margin`, `gross_margin`, `da_pct_revenue`, `capex_pct_revenue`, `nwc_pct_revenue_change`
or an exit multiple; they are not read. Everything else about the header (`years`,
`base_year`, `peers`, `drivers`, `thresholds`, `material_events`, scenario probabilities and
rationales, `view`) is as described above. All money in **millions of the reporting
currency**; rates as fractions. A single number instead of a list applies to every year.

## Regulated utility: `model.utility`

- `rate_base`: the rate base at each year end, from the company's capital plan, with
  `model.base_rate_base` for the base year (both from the filing or investor presentation).
- `equity_ratio`: the equity share of the rate base the regulator allows (often 0.45-0.55).
- `allowed_roe`: the allowed ROE from the latest rate orders, by year.
- `earned_roe`: the ROE you expect it to earn. Regulatory lag usually holds it below the
  allowed; above it needs `roe_rationale`.
- `other_income`: everything the rate base does not earn, after tax, by year: the
  unregulated and contracted businesses (a renewables developer, gas pipelines, a retail
  supplier) as positive earnings, and parent-company interest as a negative. Many "utilities"
  earn a third or more outside the rate base: the code rebuilds the base year from your
  drivers and sends the model back if it does not explain the reported net income. If an
  unregulated business deserves its own multiple, also value it in `parts`.

Net income is the earned ROE on the equity share of the average rate base, plus other income.
Book equity grows with the equity share of the rate base; when earnings less the dividend do
not fund that, the gap is **new equity issued**, which the tables show and the text must
address (the company's equity issuance guidance, ATM programs, hybrid capital).

`terminal`: `roe` (the ROE sustained after the forecast, near the allowed) and `growth` (rate
base growth for ever, below the risk-free rate).

Scenarios may change `earned_roe`, `equity_ratio`, `rate_base_change` (a level difference
from the base case, e.g. -0.10 for a smaller capital plan), `terminal` and `cost_of_equity`:
an adverse rate case and a cut capital plan are the usual bear cases.

## REIT: `model.reit`

- `noi`: net operating income of the **existing** portfolio by year, grown at same-store
  rates (occupancy, rent spreads on renewals, escalators). Do not add acquisitions here.
- `net_investment`: acquisitions plus development less dispositions, by year;
  `acquisition_cap_rate`: the initial yield new investment earns (the company's stated
  investment spread, or recent deal cap rates); `debt_funded`: the share of it funded with
  debt at the company's target leverage. The code adds the new NOI (from mid-year) and the
  interest on the new debt at the cost of new debt, so investment, NOI and interest move
  together in every scenario: growth adds value only when the cap rate beats the cost of
  the capital that funds it.
- `g_and_a`, `interest_expense` (on the **existing** debt), `maintenance_capex` (recurring
  capex, leasing costs and tenant improvements that keep the buildings let), `other_income`
  if material.
- `cap_rate` with `cap_rate_source` (a broker survey or a recent comparable sale) and
  `other_assets` (land, development at cost): code computes net asset value beside the cash
  flows and the cap rate today's price implies.

AFFO is NOI plus other income less G&A, interest and maintenance capex. What shareholders keep
is AFFO less the equity-funded share of net investment.

`new_debt_rate` (optional, with `new_debt_rate_source`): what new debt costs, from a recent
bond coupon; otherwise the code uses the risk-free rate plus 1.5%.

`terminal`: `growth` (AFFO growth for ever, below the risk-free rate). The return new
investment earns shareholders after the forecast is computed by the code from your final
cap rate, debt share and cost of new debt; give `investment_return` only to argue a
different one in `rationale`. When that return is below the cost of equity, growth
destroys value: say so in the text.

Scenarios may change `noi_change` and `net_investment_change` (level differences from the base
case, e.g. -0.4 for 40% less), `net_investment` (amounts),
`acquisition_cap_rate`,
`debt_funded`, `terminal` and `cost_of_equity`.

## The discount rate and placeholders

The code discounts at the **cost of equity** (CAPM), stated in the message; cite
`{{cost_of_equity}}`. Cite, never type: `{{intrinsic}}`, `{{upside}}`, `{{rating}}`, `{{g}}`,
`{{terminal_roe}}`, `{{book_value}}`, `{{pb}}`, `{{implied_pb}}`, `{{ddm_value}}`; per year
`{{net_income:FY2027}}`, `{{roe:FY2027}}`, `{{book_value:FY2027}}`,
`{{distributions:FY2027}}`, `{{rate_base:FY2027}}`, `{{earned_roe:FY2027}}`,
`{{allowed_roe:FY2027}}`; for a REIT `{{noi:FY2027}}`, `{{affo:FY2027}}`,
`{{affo_per_share:FY2027}}`, `{{p_affo}}`, `{{implied_p_affo}}`, `{{affo_yield}}`,
`{{nav_per_share}}`, `{{cap_rate}}`, `{{implied_cap_rate}}`, `{{investment_return}}`.

## Example `model` (utility)

```json
"model": {
  "base_year": "FY2025",
  "years": ["FY2026", "FY2027", "FY2028", "FY2029", "FY2030"],
  "base_rate_base": 80000,
  "utility": {
    "rate_base": [86000, 92500, 99000, 105500, 112000],
    "equity_ratio": 0.50,
    "allowed_roe": 0.105,
    "earned_roe": [0.098, 0.099, 0.100, 0.100, 0.100],
    "other_income": [-900, -1000, -1100, -1150, -1200]
  },
  "terminal": {"roe": 0.10, "growth": 0.03,
               "rationale": "allowed returns have held near 10.5% through two rate cycles"},
  "peers": ["DUK", "SO", "AEP"],
  "bear": {"earned_roe": 0.090, "rate_base_change": -0.08,
           "rationale": "an adverse rate case and a capital plan cut to protect bills"},
  "bull": {"earned_roe": 0.104, "rate_base_change": 0.06,
           "rationale": "data-centre load lifts the capital plan with constructive recovery"}
}
```
