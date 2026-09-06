# Matchline remote facts collector

This directory is a small, dependency-free collector for the separate VPS
`ubuntu@170.106.198.250`. It is deliberately not part of the legacy
`wc-predict` service.

The collector fetches bounded, display-only facts from two independent lanes:

- the eight allowlisted OpenFootball 2026-27 feeds (Premier League,
  Championship, Bundesliga, La Liga, Serie A, Ligue 1, Eredivisie, and
  Primeira Liga); and
- twenty-four completed OpenFootball history feeds covering the 2025-26,
  2024-25, and 2023-24 seasons across Premier League, Championship,
  Bundesliga, La Liga, Serie A, Ligue 1, Eredivisie, and Primeira Liga,
  kept in a separate historical section for form/history research; and
- ten allowlisted OpenLigaDB public competitions: Bundesliga, 2. Bundesliga,
  3. Liga, Premier League, LaLiga, Champions League, DFB Pokal, Europa League,
  Nations League A, and the 2026 World Cup.

It also records policy/transport diagnostics for Wikidata, MET Norway, ESPN,
and SofaScore. It writes one atomically replaced JSON file and contains no
prediction, odds, model, or market values. Provider payloads are hashed for
lineage but are not copied into the cache. OpenFootball is CC0; OpenLigaDB is
ODbL and remains a separately attributed, facts-only lane.

Run once:

```bash
python3 /home/ubuntu/matchline-facts/collector.py \
  --output /home/ubuntu/matchline-facts/current.json
```

The installed `matchline-facts-collector.timer` runs the same command every
15 minutes with a private (`0600`) output. The collector does not POST to
Sites; an explicit authenticated import step is required before the cache can
become public.

## Import to the Sites server

The relay is intentionally a separate, authenticated step. After the
`/api/v1/remote-facts` route has been saved and deployed, put the ingest token
in a VPS-only environment file (mode `0600`) and run the publisher as the
`ubuntu` user. The current installation uses
`/home/ubuntu/matchline-facts/publisher.env`:

```bash
set -a; . /home/ubuntu/matchline-facts/publisher.env; set +a
python3 /home/ubuntu/matchline-facts/publish_remote_facts.py \
  --input /home/ubuntu/matchline-facts/current.json
```

The publisher accepts only the fixed canonical Sites endpoint, validates the
facts-only envelope, and prints counts plus a body hash; it never prints the
token or sends model, odds, or raw provider payloads. After the first manual
import returns `status=ok` and the public read route has been checked, the
companion `matchline-facts-publisher.service` and `.timer` units can be enabled
for a 15-minute server-side relay. A failed import leaves the last server
object unchanged.

When upgrading an older installation, keep only this timer enabled. The
legacy `matchline-facts.timer` targets the same output path and must remain
disabled so two jobs cannot replace the snapshot concurrently.

`MATCHLINE_MET_COORDINATES` may contain a small JSON object such as
`{"Berlin":[52.52,13.405]}`. The installed collector systemd unit loads the
VPS-only `collector.env` so these bounded weather observations are actually
collected; coordinates are display-only and must not be treated as model
features. A successful snapshot records the history season, row counts, and
source hashes so the UI can show exactly what is available instead of a red
unpublished placeholder.
