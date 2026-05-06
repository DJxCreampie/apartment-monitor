# Apartment Availability Monitor

Simple Python + Playwright monitor that checks apartment listing pages, detects unit numbers, and sends a Discord alert when a **new unit number** appears.

## What it does

- Monitors multiple URLs from `config.yaml`
- Uses Playwright Chromium in headless mode for JavaScript-rendered pages
- Extracts visible page text from each URL
- Detects unit numbers using regex (global default or per-property custom regex)
- Compares current units to previously seen units in `seen_units.json`
- Sends Discord webhook alerts for **new unit numbers only**
- Ignores rent/move-in/date/other page content changes
- Saves and commits updated `seen_units.json` from GitHub Actions runs

## Files

- `monitor.py` — main script
- `config.yaml` — monitored properties
- `seen_units.json` — state file of previously seen units per URL
- `.github/workflows/monitor.yml` — runs monitor every 15 minutes and commits state changes

## 1) Create a Discord webhook

1. In Discord, open your server settings.
2. Go to **Integrations** → **Webhooks**.
3. Create a webhook and copy its URL.

## 2) Add `DISCORD_WEBHOOK_URL` as a GitHub Secret

1. In your GitHub repo, open **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret**.
3. Name: `DISCORD_WEBHOOK_URL`
4. Value: paste your Discord webhook URL.

## 3) Add URLs to `config.yaml`

`config.yaml` supports multiple properties:

```yaml
properties:
  - name: "MAA Test Property"
    url: "https://www.maac.com/available-apartments/?propertyId=611791"
    unit_regex: "\\b(?:Unit|Apt|Apartment)\\s*#?\\s*([A-Za-z0-9-]{2,8})\\b"

  - name: "Another Property"
    url: "https://example.com/apartments"
    # Optional: override regex if site has different unit formatting
    # unit_regex: "\\b(?:#|Unit)\\s*([A-Za-z0-9-]{2,8})\\b"
```

Notes:
- `unit_regex` is optional per property.
- If omitted, script uses a built-in default regex.

## 4) Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python monitor.py
```

After running, `seen_units.json` is updated with current units for each monitored URL.

## 5) Enable GitHub Actions

1. Push this repo to GitHub.
2. Open the **Actions** tab.
3. Enable workflows if prompted.
4. Run **Apartment Availability Monitor** manually once via **Run workflow**.
5. Workflow then runs automatically every 15 minutes (`*/15 * * * *`).

## Alert format

Alerts are sent as:

> New apartment unit posted at {property_name}: Unit {unit_number}. URL: {url}
