# Replication Market — Trader Scout

Daily-refreshed screening of Polymarket traders who are:
- **Actually active**: at least 10 trades in the last 7 days (not a
  one-big-lucky-bet-3-months-ago account sitting idle since).
- **Really profitable, not just once**: real realized profit (from
  Polymarket's own `/closed-positions` data, not a naive trade-flow
  guess) in the **7-day, 30-day, and 60-day** windows all at once.
- **Diversified**: the final list mixes traders from different market
  categories (sports, politics, macro/finance, entertainment, esports)
  rather than being dominated by one vertical.

This is separate from the main `polymarket-copy-platform` app on purpose
(the owner asked not to touch that site for this) - a standalone
screening tool whose only job is to answer "who should we actually be
featuring/considering," refreshed once a day via cron on the same VPS
that already has reliable direct Polymarket access.

Dashboard: https://giuseppemineo685-beep.github.io/replication-market-trader-scout/

## Why this exists

Polymarket's own leaderboard, ordered by monthly PNL, is what
`polymarket-copy-platform`'s Discover page shows today - but a trader who
placed one huge bet 3 months ago that only just resolved shows up there
with a big number and zero recent activity. This tool exists specifically
to filter that pattern out.

## How it runs

`scripts/scout.py` on the Germany VPS (`178.105.143.153`, same box as the
`atlantis-polymarket-screening` bots - direct Polymarket access already
confirmed reliable there), via a daily cron entry. Pulls a candidate pool
from Polymarket's leaderboard, screens each wallet's real trades and
closed positions, writes `docs/index.html`, commits, and pushes.
