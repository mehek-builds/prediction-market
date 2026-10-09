# Rollback plan

What to do when a release or launch day goes wrong. Work top to bottom: stop the bleeding, then roll back, then
find out why.

## 0. Before launch (once)

- Tag every release so there is something to roll back to: `git tag v0.2.0 7aea710 && git push --tags`, and tag each
  new release as it ships (`git tag vX.Y.Z && git push --tags`).
- Run the restore drill and keep it green: `python3 -m fastlane.backup && python3 -m fastlane.backup --verify`.
- Set a credit limit on the OpenRouter key (openrouter.ai, Keys) and keep `JEV_MAX_USD_PER_DAY` set.
- Make sure crash reports reach you: fill in `MAINTAINER_DSN_*` in `fastlane/errors.py` (from your Sentry project's
  DSN), set `FASTLANE_TELEMETRY=1`, start the engine and check that the first line says reports go to the maintainer, then
  confirm the project receives a test event (`python3 -c "from fastlane import errors; errors.init('test');
  errors.message('rollback drill', 'drill'); errors.flush()"`).
- Write down the last known good tag here: **v0.2.0**.

## 1. Stop the bleeding (minutes)

| Symptom | Do this now |
|---|---|
| Real orders going wrong (unexpected orders, wrong side, too large) | Dashboard **Back to paper**, or `python3 -m fastlane.live paper`. Then stop the engine (Ctrl-C / `docker stop`). If either fails, revoke the API key at kalshi.com (Account, API keys): no key, no orders. Orders are immediate-or-cancel, so nothing is left resting; check open positions on kalshi.com. |
| OpenRouter bill climbing | Stop the engine. Lower `JEV_MAX_CALLS_PER_HOUR` / `JEV_MAX_USD_PER_DAY`, or lower the key's credit limit at openrouter.ai. |
| Engine crashing in a loop | Stop it (`docker stop`, or Ctrl-C). Read `fastlane/results/errors.log` and Sentry. Roll back the code (section 2). |
| Dashboard broken or erroring | The engine keeps trading on paper without it. Roll back the code (section 2) and restart only the API. |
| Vercel copy broken or exposed | `vercel rollback` from `fastlane/results/vercel/<project>` (or the Vercel dashboard, Deployments, Instant Rollback). To take it down: `vercel remove <project> --yes`. To change the password: `python3 -m fastlane.deploy --password NEW`. |
| Ledger corrupted or wrong | Stop the engine, then restore (section 3). |

Users running their own copy: tell them to run `git checkout <last good tag>` and restart, or to pull the fix
once it is out. Post the same instruction wherever the release was announced.

## 2. Roll back the code

```bash
python3 -m fastlane.live paper                 # if real trading might be on
# stop the engine and the API (Ctrl-C, or: docker stop <containers>)
python3 -m fastlane.backup                     # snapshot the ledger as it is now
git fetch --tags && git checkout v0.2.0        # the last known good tag
pip install -r requirements.txt
python3 -m pytest -q                           # must pass before restarting
python3 -m fastlane.run                        # starts on paper, always
```

Docker: `docker build -t fastlane:v0.2.0 .` from the checked-out tag and rerun the same `docker run` lines.

The ledger is forward and backward compatible across these versions: new releases only add tables and columns
(`CREATE ... IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN`), and older code ignores what it does not know. So a code
rollback does not need a ledger rollback. If a future release ever changes that, it must say so in CHANGELOG.md and
in this file.

v0.5.0 ledger changes are additive (new columns on `decisions` and `trades`, new tables `paper_orders`, `paper_fills`,
`releases`, `release_markets`, `bls_requests`): v0.4.0 reads the ledger and ignores them, and working paper orders are
abandoned on rollback (they are paper). One caveat: v0.4.0 does not know the starter book, whose trades carry
`shadow = 0`, so a v0.4.0 binary counts them with the real paper book; roll back with the starter book empty or
ignore its rows (`trades.book = 'starter'`).

To ship the fix: fix forward on a branch, run the tests, tag a new patch version, and only then tell users to move.

## 3. Roll back the data

```bash
# stop the engine first: restore refuses while the engine heartbeat is fresh
python3 -m fastlane.backup --list                          # pick a backup from before the problem
python3 -m fastlane.backup --verify fastlane/results/backups/ledger-YYYYmmdd-HHMMSS.db.gz
python3 -m fastlane.backup --restore fastlane/results/backups/ledger-YYYYmmdd-HHMMSS.db.gz --yes
```

The ledger being replaced is kept next to it as `ledger.db.pre-restore-<time>`, so a restore can itself be undone.
Real-order rows (`live_orders`) are carried over from the ledger being replaced, so the real-money caps and the
one-position-per-market block survive a restore; when the same order is in both, the current ledger's newer row wins.
They are lost only if the current ledger itself is gone (deleted or corrupted). In that case real orders placed after
the backup's time are still real on Kalshi, so reconcile against kalshi.com (Portfolio, History) before turning real
trading back on.

## 4. After

- Find the cause in Sentry and `errors.log` (each report carries a `where` tag: `engine.handle`, `live.buy`,
  `api /trades`, ...).
- Add a test that fails on the bug, fix it, tag a patch release, update the last known good tag above.
- Write down what happened and what changed, in CHANGELOG.md under the patch release.
