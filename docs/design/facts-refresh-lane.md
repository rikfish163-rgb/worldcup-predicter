# Facts refresh lane

Matchline keeps the facts path independent from the model path:

```text
OpenFootball CC0 raw archive (durable)
        │ verified replay + current adapters
        ▼
facts_snapshot_refresh (shared cycle lock)
        │ atomic current.json in the runtime root
        ├── raw-artifact-upload → Sites R2/D1 raw + facts index
        └── sites-sync-facts   → facts-only research fixture projection
```

The refresh task never reads a model lock, runs a prediction, or changes a
publication epoch.  It can therefore continue to update the schedule while a
prospective window is correctly blocked by lock drift.  Only the OpenFootball
raw archive is a training/evidence boundary; Wikidata venue and MET weather
are bounded display facts, and rights-blocked providers remain empty with an
explicit status.

## Storage policy

- Keep raw OpenFootball bytes and their manifest on the durable runtime disk.
- Keep the current pointer and short-lived derived snapshot in the configured
  runtime root; do not copy it into `public/` or a browser asset.
- Let the existing uploader send content-addressed raw/facts objects to Sites;
  the public API exposes only the allow-listed facts projection.
- Retain runtime archives through the existing archive/checkpoint policy; do
  not grow an unbounded second archive for this lane.

## Operator cutover

The unit files are intentionally installed-but-not-enabled.  After confirming
the raw archive, filesystem headroom, and a quiet write window, enable the
timer together with the existing facts uploader:

```bash
systemctl --user daemon-reload
systemctl --user enable --now matchline-facts-snapshot-refresh.timer
systemctl --user enable --now matchline-sites-sync-facts.timer
```

Before enabling, run one bounded manual check with an isolated output path and
confirm that the returned summary says `lane=facts_only` and
`model_lane=not_run`.  A refresh failure must leave the prior pointer intact;
the task is not a replacement for generating and validating a new prospective
model lock.
