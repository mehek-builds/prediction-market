# Changelog

## 0.2.0 - 2026-10-09

### Added
- Cost filter (real rule and shadow): `PASS too_expensive` when the live book's held-side spread is over
  `MAX_SPREAD_CENTS` (default 3) or the round-trip cost (spread plus entry and exit Kalshi taker fees per contract)
  exceeds `COST_TO_ROOM_MAX` (default 0.25) of the room to profit. Applied after selection; thresholds unchanged.
- Shadow mode: a separate paper book recording what a looser rule (`SHADOW_SIGNAL_THRESHOLD`, default 0.60;
  `SHADOW_DECISIVE_MIN`, default 0.0; `SHADOW_ENABLED`) would have traded on the same Jev answers, only when the real
  rule had no buy intent. Same freshness guards, cost filter, fill simulation, fee and sizing; own
  one-position-per-market rule; never counts toward bankroll, the daily loss halt or real positions. P&L is broken
  down by signal strength bucket (0.60-0.70, 0.70-0.85, 0.85+) in the report and the dashboard.
- Ledger: `trades.shadow`, `trades.signal_strength`, `trades.signal_decisive`,
  `decisions.shadow_action/shadow_reason/shadow_market_id` (old ledgers migrate on open); shadow marks keyed
  `shadow:<event_id>`.
- API: `shadow`, `signal_strength`, `bucket` per trade; `/trades?book=all|live|shadow|test`; `summary.shadow_trades`
  and `summary.books` (shadow with `buckets`); `/decisions` gains shadow fields and `after_costs_cents`.
- Dashboard: All / Live / Shadow / Test tabs, SHADOW-tagged cards, bucket strip on the Shadow view, "Shadow bought
  YES/NO" chip, and the decision log now shows "price moved" (mid, before costs) and "after costs" (bid exit, fees).
- Report: "Shadow vs real" section by strength bucket (count, avg P&L per contract after spread and fees per
  horizon, winners/losers).

### Changed
- `decision.decide()` takes `signal_threshold` / `decisive_min` keyword parameters (defaults are the real rule, which
  is unchanged).

## 0.1.1 - 2026-10-09

### Changed
- `decision.decide()` drops qualifying candidates whose signalled side already trades at or above `books.MAX_ENTRY_PRICE`
  (95c, 5c of room or less). If every qualifier is priced like that the decision is `PASS priced_in` and the market is
  still tracked; previously such a market could be chosen and then logged as `no_fill_within_limit` by the fill guard.
- README "Trade rule and guards" documents the no-room fallback.

## 0.1.0 - 2026-10-09

- Initial public release.
