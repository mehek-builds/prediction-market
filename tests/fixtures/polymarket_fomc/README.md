# Polymarket FOMC fixtures

Captured live (GET only, one request at a time) on 2026-10-10, between 10:41 and 10:43 UTC (`meta.json` `captured_ts` is the
first request). Never edited by hand. Used by `tests/test_poly_fomc.py`, `tests/test_releases.py` and
`python3 -m fastlane.releases --rehearse fomc`.

Capture commands:

    curl -G "https://gamma-api.polymarket.com/public-search" --data-urlencode "q=Fed decision in October 2026" -o search-2026-10.json
    curl "https://gamma-api.polymarket.com/events?slug=fed-decision-in-october-20260617190323537" -o event-2026-10.json
    curl "https://gamma-api.polymarket.com/markets/<id>" -o markets/<id>.json          # ids 2589810 .. 2589814
    curl "https://clob.polymarket.com/book?token_id=<yes_token>" -o books/<yes_token>.json   # first clobTokenIds entry per market
    curl "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm" -o statement-2026-09-16.html
    curl "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm" -o statement-2026-07-29.html
    curl "https://www.federalreserve.gov/feeds/press_monetary.xml"                          # read to confirm item titles and dates

Notes:
- The 2026-09-16 statement moved the range from 3-1/2 to 3-3/4 percent (the 2026-07-29 statement) to 3-3/4 to 4 percent: a
  25 bp hike, so `expected_bracket` is `hike_25`. The statement itself has no "from X to Y" sentence, hence the second
  statement page. `prior_range_for_statement` is the range after the 2026-07-29 statement.
- The rehearsal replays the 2026-09-16 statement as if it were the 2026-10-28 statement. Its prior-range check therefore
  reads the 2026-07-29 statement (`prior_statement_url`), which is what a real feed holds for the meeting before.
- The order books are a snapshot taken on the capture day; the rehearsal trades against those prices.
- Descriptions contain both `poly_fomc.DESCRIPTION_MUST_HAVE` substrings verbatim (confirmed against the captured text).
