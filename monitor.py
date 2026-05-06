import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple

import requests
import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CONFIG_PATH = Path("config.yaml")
SEEN_PATH = Path("seen_units.json")
DEFAULT_UNIT_REGEX = r"\\b(?:Unit|Apt|Apartment)\\s*#?\\s*([A-Za-z0-9-]{2,8})\\b"
DISCORD_MAX_MESSAGE_LEN = 2000
FALSE_POSITIVES = {"details", "features", "home", "rental", "until"}


def load_config() -> Tuple[List[dict], bool]:
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

    send_heartbeat = bool(data.get("send_heartbeat", False))
    return cleaned, send_heartbeat


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
                pass
            page.wait_for_timeout(3000)
            text = page.inner_text("body")
            return text or ""
        finally:
            browser.close()


def normalize_unit_candidate(candidate: str) -> str | None:
    value = str(candidate).strip()
    if not value:
        return None

    value = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9-]+$", "", value)
    if not value:
        return None

    if value.lower() in FALSE_POSITIVES:
        return None

    if not re.search(r"\d", value):
        return None

    if not re.match(r"^\d[A-Za-z0-9-]{0,7}$", value):
        return None

    return value.upper()


def detect_units(page_text: str, unit_regex: str) -> Set[str]:
    pattern = re.compile(unit_regex, re.IGNORECASE)
    matches = pattern.findall(page_text)
    units = set()

    for match in matches:
        candidate = match[0] if isinstance(match, tuple) else match
        normalized = normalize_unit_candidate(candidate)
        if normalized:
            units.add(normalized)

    return units


def build_discord_message(property_name: str, new_units: List[str], removed_units: List[str], url: str) -> str:
    header = f"Availability changed at {property_name}\n\n"
    url_line = f"\n\nURL: {url}"

    def clamp_list(label: str, units: List[str], current_message_len: int) -> str:
        if not units:
            return f"{label}: None"

        selected: List[str] = []
        for unit in units:
            maybe = selected + [unit]
            line = f"{label}: {', '.join(maybe)}"
            if current_message_len + len(line) + len(url_line) <= DISCORD_MAX_MESSAGE_LEN:
                selected.append(unit)
            else:
                break

        omitted = len(units) - len(selected)
        line = f"{label}: {', '.join(selected) if selected else 'None'}"
        if omitted > 0:
            line += f" (+{omitted} more)"
        return line

    new_line = clamp_list("New units", new_units, len(header))
    removed_line = clamp_list("Removed units", removed_units, len(header) + len(new_line) + 2)
    return f"{header}{new_line}\n\n{removed_line}{url_line}"


def send_discord_message(webhook_url: str, message: str, context: str) -> None:
    max_attempts = 4

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(webhook_url, json={"content": message}, timeout=20)
        except requests.RequestException as exc:
            print(f"Discord send failed for {context} (attempt {attempt}/{max_attempts}): {exc}", file=sys.stderr)
            if attempt == max_attempts:
                return
            time.sleep(min(2**attempt, 10))
            continue

        if resp.status_code == 429:
            retry_after = 2.0
            try:
                payload = resp.json()
                retry_after = float(payload.get("retry_after", retry_after))
                if retry_after > 100:
                    retry_after /= 1000.0
            except (ValueError, json.JSONDecodeError, AttributeError):
                pass

            print(
                f"Discord rate-limited for {context}; retrying in {retry_after:.2f}s "
                f"(attempt {attempt}/{max_attempts}).",
                file=sys.stderr,
            )
            if attempt == max_attempts:
                print(f"Giving up Discord alert for {context} after repeated 429 responses.", file=sys.stderr)
                return
            time.sleep(max(retry_after, 0.5))
            continue

        if 200 <= resp.status_code < 300:
            return

        print(f"Discord send failed for {context} with HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return


def send_discord_alert(webhook_url: str, property_name: str, new_units: List[str], removed_units: List[str], url: str) -> None:
    message = build_discord_message(property_name, new_units, removed_units, url)
    send_discord_message(webhook_url, message, f"property {property_name}")


def build_heartbeat_message(property_count: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "Apartment monitor heartbeat\n"
        f"Checked: {property_count} properties\n"
        f"Time: {timestamp}\n"
        "No unit changes detected."
    )


def send_heartbeat(webhook_url: str, property_count: int) -> None:
    message = build_heartbeat_message(property_count)
    send_discord_message(webhook_url, message, "heartbeat")


def main() -> int:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set; skipping alerts.", file=sys.stderr)

    properties, heartbeat_enabled = load_config()
    seen_units_by_url = {k: set(v) for k, v in load_seen_units().items()}

    had_unit_changes = False

    for prop in properties:
        name = prop["name"]
        url = prop["url"]
        unit_regex = prop["unit_regex"]

        print(f"Checking {name} ({url})")
        text = extract_visible_text(url)
        current_units = detect_units(text, unit_regex)

        prior_units = seen_units_by_url.get(url, set())
        new_units = sorted(current_units - prior_units)
        removed_units = sorted(prior_units - current_units)

        if new_units or removed_units:
            had_unit_changes = True
            print(f"Availability changed for {name}. New: {new_units or ['None']} Removed: {removed_units or ['None']}")
            if webhook_url:
                send_discord_alert(webhook_url, name, new_units, removed_units, url)

        seen_units_by_url[url] = current_units

    if heartbeat_enabled and webhook_url and not had_unit_changes:
        send_heartbeat(webhook_url, len(properties))

    save_seen_units(seen_units_by_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
