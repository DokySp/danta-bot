# Local Strategy Signal Rules

## Boundary

Strategy signals are calculated by the market collector from the one-pass market snapshot. After `market.json` is finalized, no strategy tool, KIS API, web call, backtest service, or other external source may be called for any verdict stage.

## Signals

Calculate signals only when the required chart fields exist. Otherwise emit `ERROR` with the missing fields.

| Signal | Required data | BUY example | SELL example |
|---|---|---|---|
| Moving-average trend | daily closes | short MA above long MA | short MA below long MA |
| Medium trend | weekly closes | rising weekly trend | falling weekly trend |
| Long trend | monthly closes | rising monthly trend | falling monthly trend |
| 52-week position | price and 52-week high/low | near high with support | failed breakout or near low with deterioration |
| Momentum | recent closes | positive sustained return | negative sustained return |
| Volume confirmation | price and volume | rise with expanding volume | fall with expanding volume |
| Volatility risk | highs/lows/closes | controlled volatility | unstable expansion or gap risk |
| Investor flow | investor-flow snapshot | aligned net buying | persistent net selling |
| ETF/NAV | ETF price and NAV | acceptable premium/discount | abnormal premium/discount or tracking risk |

## Output

```json
{
  "signal": "BUY | HOLD | SELL | ERROR",
  "strength": 0.0,
  "reason": "",
  "input_fields": [],
  "calculated_at": ""
}
```

- `strength` is between `0.0` and `1.0`.
- Cite the exact collected field names and observation dates.
- Do not replace missing values with another symbol's data.
- Signals support verdicts but do not directly authorize orders.
