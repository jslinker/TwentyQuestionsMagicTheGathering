# Local data

This directory contains downloaded inputs used to build the decision tree. The
large data files are intentionally excluded from Git and can be recreated with:

```sh
python3 scripts/pipeline.py refresh
```

- `card-data.json` is Scryfall's normalized Oracle Cards bulk payload.
- `card-data.metadata.json` records its download provenance and hashes.
- `snapshots/` contains timestamped Scryfall Tagger snapshots for reproducible builds.

Do not deploy this directory. The compact browser-ready files are generated at
the repository root for GitHub Pages.
