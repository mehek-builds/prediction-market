# Changelog

## 0.1.1 - 2026-10-09

### Changed
- `decision.decide()` drops qualifying candidates whose signalled side already trades at or above `books.MAX_ENTRY_PRICE`
  (95c, 5c of room or less). If every qualifier is priced like that the decision is `PASS priced_in` and the market is
  still tracked; previously such a market could be chosen and then logged as `no_fill_within_limit` by the fill guard.
- README "Trade rule and guards" documents the no-room fallback.

## 0.1.0 - 2026-10-09

- Initial public release.
