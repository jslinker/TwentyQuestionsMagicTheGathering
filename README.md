# Twenty Questions: Magic The Gathering

A static yes/no game that guesses a Magic: The Gathering card. The decision tree is generated locally from Scryfall card data and a pinned Scryfall Tagger snapshot; the finished site has no database or application server.

The game asks card-property and semantic questions first. If those questions leave multiple candidates, the UI announces a **Name tiebreaker** phase and finishes with deterministic questions about the printed card name.

## Requirements

- Python 3.10 or newer
- `curl`
- Approximately 250 MB of free working space for the uncompressed source data and generated files

There are no third-party Python packages to install. Run every command below from the project directory.

## Quick start with fresh data

Download fresh card and Tagger data, rebuild the tree, validate every card route, and package the static site:

```sh
python3 scripts/pipeline.py refresh
```

Start the local web server:

```sh
python3 scripts/pipeline.py serve
```

Open <http://localhost:8000> and press **Start Game**. Press Control-C in the terminal to stop the server.

Do not open `index.html` directly as a `file://` URL. Browsers commonly block its JSON requests in that mode; use the local HTTP server.

## Downloading the Scryfall card dump

Run:

```sh
python3 scripts/fetch_card_data.py
```

This creates or replaces:

- `data/card-data.json` — the current card dataset;
- `data/card-data.metadata.json` — source URL, Scryfall update time, selected payload type, record counts, and SHA-256 hash.

The script first requests Scryfall’s [bulk-data catalog](https://scryfall.com/docs/api/bulk-data), selects the entry whose machine-readable `type` is exactly `oracle_cards`, and then follows that entry’s current `download_uri`. Download URLs are not hardcoded because Scryfall changes them as bulk files are updated.

### Why `oracle_cards` is required

Scryfall offers several bulk payloads:

| Payload | Contents | Use here? |
|---|---|---|
| `oracle_cards` | One representative card object per Oracle ID | **Yes** |
| `default_cards` | English/printed-language card objects for individual printings | No; it introduces printing duplicates |
| `all_cards` | Every printing in every language | No; it introduces many printing and language duplicates |
| `unique_artwork` | Objects selected to represent distinct artwork | No; the game guesses cards, not artworks |
| `rulings` | Ruling objects rather than card objects | No |

The downloader refuses to continue unless it finds exactly one `oracle_cards` catalog entry. It supports both Scryfall’s array-style `download_uri` and its newer `jsonl_download_uri`. JSON Lines downloads may be gzip-compressed; the script detects the gzip header, decompresses the response, parses each JSON Lines record, and normalizes the result into the JSON array expected by the generator. If neither file URI is present, it checks the type-specific `oracle-cards` metadata endpoint and finally its file response. It accepts HTTPS URLs, protocol-relative URLs, and legacy HTTP URLs upgraded to HTTPS.

After decoding, the downloader verifies that the payload is non-empty and that every record has a unique `oracle_id`. That uniqueness check also protects the project from accidentally accepting a print-level dump. The metadata sidecar records the URI field used, source format, compression, response hash, and normalized-output hash.

The official bulk file contains tokens, planes, schemes, Vanguard cards, emblems, and other external game pieces. They remain in the source snapshot but are deterministically excluded during tree generation.

## Downloading Scryfall Tagger data

Run:

```sh
python3 scripts/fetch_oracle_tags.py
```

This writes an immutable timestamped file such as:

```text
data/snapshots/oracle-tags-snapshot-20260818-230352Z.json
```

The snapshot contains Oracle tag labels and their associated Oracle IDs. Duplicate tag-label rows are valid and deliberately preserved; the generator unions their Oracle IDs when it loads the snapshot.

Unlike Scryfall’s documented bulk-card catalog, the Tagger endpoint is private and undocumented. A future Scryfall change may require updating `scripts/fetch_oracle_tags.py`. The live endpoint is used only by the explicit download command—the game and tree generator never depend on it at runtime.

Both downloaders use the system `curl` executable by default. This avoids the Python certificate-store failure that occurs on some macOS installations. To try Python networking instead, pass `--transport urllib`; to try Python first and fall back to curl, pass `--transport auto`.

## Building the game

Build from the card data and newest local Tagger snapshot:

```sh
python3 scripts/pipeline.py build
```

For a reproducible build pinned to a particular Tagger snapshot:

```sh
python3 scripts/pipeline.py build --snapshot data/snapshots/oracle-tags-snapshot-YYYYMMDD-HHMMSSZ.json
```

The build performs all of the following:

1. Loads the Oracle Cards dump.
2. Excludes tokens and other non-deck game pieces.
3. Loads the curated tags in `config/semantic-questions.json` from the selected Tagger snapshot.
4. Generates the card-property decision tree.
5. Adds deterministic word/alphabetical tiebreakers to semantic dead ends.
6. Simulates every included card through the finished tree and rejects incorrect routes.
7. Writes the compact browser assets to the repository root for GitHub Pages.
8. Verifies every result name and image lookup used by the browser.

To verify the already-built root site without rebuilding:

```sh
python3 scripts/pipeline.py verify
```

## Refresh options

The pipeline provides these repeatable workflows:

| Command | Result |
|---|---|
| `python3 scripts/pipeline.py refresh` | Download fresh Oracle Cards and Tagger data, then build and verify. |
| `python3 scripts/pipeline.py refresh-tags` | Keep the current card dump, download fresh Tagger data, then build and verify. |
| `python3 scripts/pipeline.py build` | Build with the current card dump and newest local Tagger snapshot. |
| `python3 scripts/pipeline.py build --snapshot FILE` | Build with an explicitly selected Tagger snapshot. |
| `python3 scripts/pipeline.py verify` | Verify the root GitHub Pages artifacts without rebuilding. |
| `python3 scripts/pipeline.py serve` | Serve the repository-root site at `http://localhost:8000`. |
| `python3 scripts/pipeline.py serve --port 9000` | Serve locally on a different port. |

## Repository structure

```text
config/              Maintained question and tag configuration
data/                Downloaded datasets and reproducible snapshots
scripts/             Download, analysis, build, and verification tools
build/               Generated decision tree and build reports
index.html            GitHub Pages entry point
*.json                Compact browser assets generated for GitHub Pages
```

`build/` and the large files under `data/` are generated or downloaded and intentionally ignored by Git. The root browser assets are intended to be committed so GitHub Pages can serve them directly.

## Files and generated output

Maintained project files:

- `index.html` — browser UI and GitHub Pages entry point;
- `scripts/generate_tree.py` — filtering, questions, tree generation, routing validation, and name fallback generation;
- `config/semantic-questions.json` — reviewed Tagger labels and player-facing question wording;
- `scripts/fetch_card_data.py` — official Scryfall Oracle Cards downloader;
- `scripts/fetch_oracle_tags.py` — timestamped Tagger downloader;
- `scripts/pipeline.py` — refresh, build, verification, packaging, and local hosting commands.

Downloaded inputs:

- `data/card-data.json` — current Oracle Cards payload;
- `data/card-data.metadata.json` — card-download provenance;
- `data/snapshots/oracle-tags-snapshot-*.json` — pinned Oracle-tag snapshots.

Generated output:

- `build/decision-tree.json` — complete two-phase decision tree;
- `build/build-metrics.json` — semantic coverage, depth, fallback, and tag-use metrics;
- `build/tag-opportunities.json` — optional unused-tag ranking produced by `scripts/analyze_tag_opportunities.py`;
- `decision-tree.json` — root decision tree fetched by the browser;
- `card-data.json` — compact root card lookup fetched by the browser;
- `build-metrics.json` — root build metrics used by the browser bundle;
- `build-info.json` — input hashes and build provenance.

The 150+ MB source card dump remains in `data/card-data.json` and is not committed. The root `card-data.json` is a compact lookup containing only the names, type information, and image URLs needed by the UI.

## Publishing to GitHub Pages

Configure GitHub Pages to deploy from the repository's primary branch and select **`/ (root)`** as the folder. The required `index.html`, `.nojekyll`, and compact JSON assets are all written at that level.

Before publishing, rebuild or verify the bundle:

```sh
python3 scripts/pipeline.py build
python3 scripts/pipeline.py verify
```

Commit the generated root assets along with `index.html`. Their paths are relative, so the game works both at a user site such as `username.github.io` and at a project site such as `username.github.io/repository-name/`.

## Troubleshooting

### SSL certificate failure

Use the default curl transport explicitly:

```sh
python3 scripts/pipeline.py refresh --transport curl
```

No local certificate changes are required.

### Tagger download fails with 401, 403, or 404

The Tagger endpoint is undocumented and may have changed or become unavailable. Existing timestamped snapshots remain usable:

```sh
python3 scripts/pipeline.py build --snapshot data/snapshots/oracle-tags-snapshot-YYYYMMDD-HHMMSSZ.json
```

### Port 8000 is already in use

Choose another local port:

```sh
python3 scripts/pipeline.py serve --port 9000
```

### Confirm which data produced the site

Inspect `build-info.json`. It records the Tagger snapshot filename, build time, and SHA-256 hashes for the card dump, tag snapshot, semantic configuration, and generator.
