# Eval results (2026-10-12)

Computed by code, no API keys: `prozorro-mcp eval --suite all`. The reconcile suite runs on the bundled demo snapshot.

## flags: PASS (35 labelled cases)

| kind | precision | recall | tp | fp | fn | tn |
|---|---|---|---|---|---|---|
| single_bidder | 1.000 | 1.000 | 6 | 0 | 0 | 4 |
| supplier_concentration | 1.000 | 1.000 | 4 | 0 | 0 | 4 |
| price_above_median | 1.000 | 1.000 | 4 | 0 | 0 | 5 |
| short_tender_period | 1.000 | 1.000 | 4 | 0 | 0 | 4 |

## normalize: PASS (accuracy 0.989 on 94 spellings)
- `Львивська` -> None (expected Львівська область)

## reconcile: PASS (35 aggregates checked against a recount from raw JSON, 0 mismatches)

