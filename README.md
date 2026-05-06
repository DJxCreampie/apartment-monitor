# Apartment Availability Monitor

Simple Python + Playwright monitor that checks apartment listing pages, detects apartment unit IDs, and sends Discord alerts when availability changes.

## What it does

- Monitors multiple URLs from `config.yaml`
- Uses Playwright Chromium in headless mode for JavaScript-rendered pages
- Extracts visible page text from each URL
- Detects likely unit IDs via regex + normalization/filtering
- Compares current units to previously seen units in `seen_units.json`
- Sends Discord alerts for unit additions/removals only
- Optionally sends a heartbeat message when no unit changes are detected
- Saves and commits updated `seen_units.json` from GitHub Actions runs

## Files

- `monitor.py` — main script
- `config.yaml` — monitor settings and properties
- `seen_units.json` — state file of previously seen units per URL
- `.github/workflows/monitor.yml` — runs monitor every 15 minutes and commits state changes

## Config

Example:

```yaml
send_heartbeat: false

properties:
  - name: "MAA Test Property"
    url: "https://www.maac.com/available-apartments/?propertyId=611791"
    unit_regex: "\\b(?:Unit|Apt|Apartment)\\s*#?\\s*([A-Za-z0-9-]{2,8})\\b"
```

- `send_heartbeat` (optional): set to `true` to send a Discord heartbeat after successful runs **only when no unit changes were detected**.
- `unit_regex` (optional per property): override regex if a site formats units differently.

## Discord setup

1. In Discord, open server settings.
2. Go to **Integrations** → **Webhooks**.
3. Create a webhook and copy the URL.
4. In GitHub repo settings, add secret `DISCORD_WEBHOOK_URL` with that URL.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python monitor.py
```

## Enable GitHub Actions

1. Push to GitHub.
2. Open **Actions** and enable workflows if prompted.
3. Run **Apartment Availability Monitor** manually once.
4. It will then run every 15 minutes.

## Alert formats

Availability-change alert:

```text
Availability changed at {property_name}

New units:
{comma-separated units or None}

Removed units:
{comma-separated units or None}

URL: {url}
```

Heartbeat alert (only when `send_heartbeat: true` and no unit changes were detected):

```text
Apartment monitor heartbeat
Checked: {property_count} properties
Time: {current UTC timestamp}
No unit changes detected.
```
