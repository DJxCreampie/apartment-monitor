import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

import requests
import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CONFIG_PATH = Path("config.yaml")
SEEN_PATH = Path("seen_units.json")
DEFAULT_UNIT_REGEX = r"\\b(?:Unit|Apt|Apartment)\\s*#?\\s*([A-Za-z0-9-]{2,8})\\b"


def load_config() -> List[dict]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    properties = data.get("properties", [])
    if not isinstance(properties, list):
        raise ValueError("config.yaml 'properties' must be a list")

    cleaned = []
    for item in properties:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("url")
        unit_regex = item.get("unit_regex") or DEFAULT_UNIT_REGEX
        if not name or not url:
            continue
        cleaned.append({"name": name, "url": url, "unit_regex": unit_regex})

    return cleaned


def load_seen_units() -> Dict[str, List[str]]:
    if not SEEN_PATH.exists():
        return {}
    with SEEN_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    normalized = {}
    for url, units in data.items():
        if isinstance(units, list):
            normalized[url] = sorted(set(str(u) for u in units if u))
    return normalized


def save_seen_units(data: Dict[str, Set[str]]) -> None:
    output = {url: sorted(units) for url, units in sorted(data.items())}
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
        f.write("\n")


def extract_visible_text(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                # Some pages continuously poll; best-effort wait.
                pass
            page.wait_for_timeout(3000)
            text = page.inner_text("body")
            return text or ""
        finally:
            browser.close()


def detect_units(page_text: str, unit_regex: str) -> Set[str]:
    pattern = re.compile(unit_regex, re.IGNORECASE)
    matches = pattern.findall(page_text)
    units = set()

    for match in matches:
        if isinstance(match, tuple):
            candidate = match[0]
        else:
            candidate = match
        candidate = str(candidate).strip()
        if candidate:
            units.add(candidate)

    return units


def send_discord_alert(webhook_url: str, property_name: str, unit_number: str, url: str) -> None:
    message = (
        f"New apartment unit posted at {property_name}: "
        f"Unit {unit_number}. URL: {url}"
    )
    resp = requests.post(webhook_url, json={"content": message}, timeout=15)
    resp.raise_for_status()


def main() -> int:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set; skipping alerts.", file=sys.stderr)

    properties = load_config()
    seen_units_by_url = {k: set(v) for k, v in load_seen_units().items()}

    for prop in properties:
        name = prop["name"]
        url = prop["url"]
        unit_regex = prop["unit_regex"]

        print(f"Checking {name} ({url})")
        text = extract_visible_text(url)
        current_units = detect_units(text, unit_regex)

        prior_units = seen_units_by_url.get(url, set())
        new_units = sorted(current_units - prior_units)

        for unit in new_units:
            print(f"New unit found for {name}: {unit}")
            if webhook_url:
                send_discord_alert(webhook_url, name, unit, url)

        seen_units_by_url[url] = current_units

    save_seen_units(seen_units_by_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
