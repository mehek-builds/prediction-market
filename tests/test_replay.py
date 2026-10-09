"""Replay: stored real-rule signals against live-quote selection. Read only, seeded ledger, no network."""
import json
import shutil
import sqlite3
import time

import pytest

from fastlane import replay
from fastlane.replay import analyse, summary_line

GOOD = {"decisive_yes": .8, "toward_yes": .15, "no_signal": .05}       # strength .95, decisive .8
WEAK = {"decisive_yes": .3, "toward_yes": .35, "no_signal": .35}


@pytest.fixture
def universe(tiny_universe):
    a2 = {"venue": "kalshi", "id": "AVNT-2", "question": "Avient quarterly earnings beat estimates", "category": "Companies",
          "yes_ask": .45, "yes_bid": .43, "volume_24h": 200}
    tiny_universe._set(tiny_universe.markets + [a2])
    return tiny_universe


HEADLINE = "Avient quarterly earnings beat"


def _decision(L, eid, now, reason, market_id="AVNT-1", answers=None, action="PASS", offset=60, synthetic=False):
    L.event({"id": eid, "source": "cnbc", "headline": HEADLINE, "summary": "", "seen_ts": now - offset,
             "published_ts": now - offset - 5, "synthetic": synthetic})
    ans = {"m0": {"probabilities": GOOD}, "m1": {"probabilities": {"no_signal": 1.0}}} if answers is None else answers
    L.decision(eid, action=action, reason=reason, venue="kalshi", market_id=market_id, market_question="Q",
               answers=ans, p_up=.95, p_down=0.0, decided_ts=now - offset)


def seeded(tmp_ledger):
    L, now = tmp_ledger, time.time()
    both = {"m0": {"probabilities": GOOD}, "m1": {"probabilities": {"decisive_yes": .6, "toward_yes": .3, "no_signal": .1}}}
    # 1: chosen market was 97c live -> would now be dropped, nobody else qualifies
    _decision(L, "d1", now, "priced_in", offset=100)
    L.mark("d1", 0, .97, .95, .96)
    # 2: chosen 97c, a second qualifier quoted 40c on the tape at decision time -> another qualifier had room
    _decision(L, "d2", now, "priced_in", answers=both, offset=90)
    L.mark("d2", 0, .97, .95, .96)
    L.tick("AVNT-2", now - 90 - 10, .38, .40)
    # 3: blocked, no stored book at all -> unknown
    _decision(L, "d3", now, "no_book", offset=80)
    # 4: the stored market is not what the shortlist rebuilds -> drifted -> unknown
    _decision(L, "d4", now, "priced_in", market_id="GONE-1", offset=70)
    L.mark("d4", 0, .97, .95, .96)
    # 5: blocked for another reason but the chosen market still had room
    _decision(L, "d5", now, "stale_news", action="BUY_YES", offset=60)
    L.mark("d5", 0, .55, .53, .54)
    # 6: a normal fill (not blocked), 7: weak signal (not qualified), 8: synthetic (excluded)
    _decision(L, "d6", now, "signal_yes", action="BUY_YES", offset=50)
    _decision(L, "d7", now, "weak_signal", answers={"m0": {"probabilities": WEAK}}, offset=40)
    _decision(L, "d8", now, "priced_in", offset=30, synthetic=True)
    L.mark("d8", 0, .97, .95, .96)
    return L, now


def test_replay_categories_on_a_seeded_ledger(tmp_ledger, universe):
    L, now = seeded(tmp_ledger)
    db = sqlite3.connect(L.path)
    lines, s = analyse(db, now - 3600, universe)
    assert s["qualified"] == 6 and s["blocked"] == 5
    assert dict(s["by_reason"]) == {"priced_in": 3, "no_book": 1, "stale_news": 1}
    assert (s["dropped"], s["alt_room"], s["unknown"], s["kept"]) == (1, 1, 2, 1)
    text = "\n".join(lines)
    assert "chosen would now be dropped" in text and "another qualifier had room" in text and "AVNT-2 0.4" in text
    assert "shortlist_drifted" in text and "no stored book at decision" in text and "chosen market still has room" in text
    assert len(lines) == 5
    assert summary_line(s) == ("qualified 6 | blocked 5 (no_book 1, priced_in 3, stale_news 1) | chosen would now be dropped 1 "
                               "| another qualifier had room per tape 1 | unknown 2 | chosen still had room 1")


def test_replay_since_window_excludes_older_decisions(tmp_ledger, universe):
    L, now = seeded(tmp_ledger)
    _, s = analyse(sqlite3.connect(L.path), now - 75, universe)       # only decisions newer than 75 s: d4..d8
    assert s["qualified"] == 3 and s["blocked"] == 2


def test_alternative_priced_from_the_shadow_or_starter_mark_when_no_tick(tmp_ledger, universe):
    L, now = tmp_ledger, time.time()
    both = {"m0": {"probabilities": GOOD}, "m1": {"probabilities": {"decisive_yes": .6, "toward_yes": .3, "no_signal": .1}}}
    _decision(L, "s1", now, "priced_in", answers=both, offset=50)
    L.mark("s1", 0, .97, .95, .96)
    L.mark("starter:s1", 0, .35, .33, .34)
    L.db.execute("UPDATE decisions SET starter_market_id = 'AVNT-2' WHERE event_id = 's1'")
    _, s = analyse(sqlite3.connect(L.path), now - 3600, universe)
    assert s["alt_room"] == 1


def test_replay_empty_ledger_and_old_schema(tmp_path, old_ledger_path, universe):
    lines, s = analyse(sqlite3.connect(old_ledger_path), 0, universe)         # no answers column data, no new columns
    assert lines == [] and s["qualified"] == 0
    assert summary_line(s).startswith("qualified 0 | blocked 0 (none)")


def test_main_is_read_only_and_prints_header_and_summary(tmp_ledger, universe, tmp_path):
    L, now = seeded(tmp_ledger)
    L.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    copy = tmp_path / "copy.db"
    shutil.copyfile(L.path, copy)
    cache = tmp_path / "universe.json"
    cache.write_text(json.dumps(universe.markets))
    before = copy.read_bytes()
    out = []
    assert replay.main(["--ledger", str(copy), "--universe", str(cache), "--since-hours", "1"], out=out.append) == 0
    text = "\n".join(out)
    assert "eight candidate books are not stored" in text and "qualified 6 | blocked 5" in text
    assert copy.read_bytes() == before


def test_main_missing_ledger_returns_one(tmp_path):
    out = []
    assert replay.main(["--ledger", str(tmp_path / "nope.db")], out=out.append) == 1 and "No ledger" in out[0]


def test_load_universe_without_a_cache_is_empty(tmp_path):
    assert replay.load_universe(tmp_path / "missing.json").markets == []
