# WikiJS to Outline Migration Tool

WIkiJS to Outline migration tool. Written by AI, it seems to work as expected :).

Published under MIT license.

No warranty. Use at your own risk.

## Usage

### Step 1: Export data from WikiJS

**Warning: WikJS Git Backup is broken! In some cases it does not export all the data!**

Use `wikijs_graphql_complete_exporter.py` to export all pages and assets from WikiJS:

```bash
python wikijs_graphql_complete_exporter.py --wiki-url https://your-wiki.com --token YOUR_API_TOKEN --output-dir ./wikiexport
```

**Parameters:**
- `--wiki-url`: WikiJS instance URL
- `--token`: WikiJS API token (generate in admin panel)
- `--output-dir`: Directory for exported data (default: `./wikijs-complete-export`)
- `--assets-only`: Optional flag to download only assets, skip pages

**Output:**
- Markdown files with YAML frontmatter
- Assets (images, files) in original folder structure
- `_export_manifest.json` with export metadata
- `_failed_assets.csv` and `_failed_assets_log.md` for failed exports

### Step 2: Upload to Outline

Use `wikijs_to_outline.py` to migrate exported data to Outline:

```bash
python wikijs_to_outline.py --outline-url https://your-outline.com --token YOUR_OUTLINE_TOKEN --wiki-dir ./wikiexport
```

**Parameters:**
- `--outline-url`: Outline instance URL
- `--token`: Outline API token
- `--wiki-dir`: Directory containing WikiJS export data

**Process:**
1. Creates "WikiJS Import" collection in Outline
2. Uploads all documents with hierarchy preservation
3. Uploads and links images/attachments
4. Updates internal crosslinks between pages

**Output:**
- `_outline_migration_log.md`: Detailed migration log
- `_outline_migration_failures.csv`: Failed imports summary

## Requirements

Create Virtual environment and install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

