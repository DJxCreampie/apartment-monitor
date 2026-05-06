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
DISCORD_MAX_MESSAGE_LEN = 1900
DISCORD_INTER_MESSAGE_DELAY_SECONDS = 0.7
FALSE_POSITIVES = {"details", "features", "home", "rental", "until"}
SUSPICIOUS_REMOVAL_RATIO = 0.80
BEDS_PATTERN = re.compile(r"^\d+\s+Beds?$$", re.IGNORECASE)
BATHS_PATTERN = re.compile(r"^\d+(?:\.\d+)?\s+Baths?$$", re.IGNORECASE)
SQFT_PATTERN = re.compile(r"^[\d,]+\s+sq\s*ft$$", re.IGNORECASE)
FLOOR_PATTERN = re.compile(r"^(?:\d+(?:st|nd|rd|th)\s+Floor|Terrace Level|Ground Floor)$$", re.IGNORECASE)
MOVE_IN_PATTERN = re.compile(r"^\d{2}/\d{2}\s*-\s*\d{2}/\d{2}$$")
RENT_PATTERN = re.compile(r"\$[\d,]+")


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


def normalize_unit_record(unit: str, beds: str = "", baths: str = "", sqft: str = "", floor: str = "", move_in: str = "", rent: str = "") -> dict:
    return {
        "unit": unit,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "floor": floor,
        "move_in": move_in,
        "rent": rent,
    }


def load_seen_units() -> Dict[str, Dict[str, dict]]:
    if not SEEN_PATH.exists():
        return {}

    with SEEN_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {}

    normalized: Dict[str, Dict[str, dict]] = {}
    for url, value in data.items():
        url_units: Dict[str, dict] = {}

        if isinstance(value, list):
            # Backward compatibility with old format: ["1315", ...]
            for unit in value:
                normalized_unit = normalize_unit_candidate(str(unit))
                if not normalized_unit:
                    continue
                url_units[normalized_unit] = normalize_unit_record(normalized_unit)

        elif isinstance(value, dict):
            for key, unit_obj in value.items():
                unit_id = normalize_unit_candidate(str(key))
                if not unit_id:
                    continue

                if isinstance(unit_obj, dict):
                    unit_val = normalize_unit_candidate(str(unit_obj.get("unit", unit_id))) or unit_id
                    beds = str(unit_obj.get("beds", "")).strip()
                    baths = str(unit_obj.get("baths", "")).strip()
                    sqft = str(unit_obj.get("sqft", "")).strip()
                    floor = str(unit_obj.get("floor", "")).strip()
                    move_in = str(unit_obj.get("move_in", "")).strip()
                    rent = str(unit_obj.get("rent", "")).strip()
                    url_units[unit_id] = normalize_unit_record(unit_val, beds, baths, sqft, floor, move_in, rent)
                else:
                    url_units[unit_id] = normalize_unit_record(unit_id)

        normalized[url] = url_units

    return normalized


def save_seen_units(data: Dict[str, Dict[str, dict]]) -> None:
    output: Dict[str, Dict[str, dict]] = {}
    for url in sorted(data):
        units = data[url]
        sorted_units = {}
        for unit in sorted(units):
            sorted_units[unit] = units[unit]
        output[url] = sorted_units

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


def parse_structured_units(page_text: str, unit_regex: str) -> Dict[str, dict]:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in page_text.splitlines() if ln.strip()]
    units: Dict[str, dict] = {}

    i = 0
    while i < len(lines):
        if lines[i].lower() == "unit" and i + 1 < len(lines):
            unit_id = normalize_unit_candidate(lines[i + 1])
            if not unit_id:
                i += 1
                continue

            rec = normalize_unit_record(unit_id)
            block_start = i + 2
            block_end = min(len(lines), block_start + 20)

            j = block_start
            while j < len(lines) and j < block_end:
                low = lines[j].lower()
                if low == "unit" or low == "additional unit features":
                    break
                j += 1
            block_end = j

            before_floor_end = block_end
            for k in range(block_start, block_end):
                if lines[k].lower() == "floor/bld":
                    before_floor_end = k
                    break

            for k in range(block_start, before_floor_end):
                line = lines[k]
                if not rec["beds"] and BEDS_PATTERN.match(line):
                    rec["beds"] = line
                elif not rec["baths"] and BATHS_PATTERN.match(line):
                    rec["baths"] = line
                elif not rec["sqft"] and SQFT_PATTERN.match(line):
                    rec["sqft"] = line

            for k in range(block_start, block_end - 1):
                if lines[k].lower() == "floor/bld":
                    floor_candidate = lines[k + 1]
                    if FLOOR_PATTERN.match(floor_candidate):
                        rec["floor"] = floor_candidate
                    break

            for k in range(block_start, block_end - 1):
                if lines[k].lower().startswith("move-in"):
                    move_candidate = lines[k + 1]
                    if MOVE_IN_PATTERN.match(move_candidate):
                        rec["move_in"] = move_candidate
                    break

            for k in range(block_start, block_end):
                if lines[k].lower() == "monthly":
                    for m in range(k + 1, block_end):
                        rent_match = RENT_PATTERN.search(lines[m])
                        if rent_match:
                            rec["rent"] = rent_match.group(0)
                            break
                    break

            if not rec["rent"]:
                for k in range(block_start, block_end):
                    rent_match = RENT_PATTERN.search(lines[k])
                    if rent_match:
                        rec["rent"] = rent_match.group(0)
                        break

            units[unit_id] = rec
            i = block_end
            continue

        i += 1

    fallback_units = detect_units(page_text, unit_regex)
    for unit_id in fallback_units:
        units.setdefault(unit_id, normalize_unit_record(unit_id))

    return units


def format_unit_line(record: dict) -> str:
    parts = [f"Unit {record.get('unit', '')}".strip()]
    if record.get("beds"):
        parts.append(record["beds"])
    if record.get("baths"):
        parts.append(record["baths"])
    if record.get("sqft"):
        parts.append(record["sqft"])
    if record.get("floor"):
        parts.append(record["floor"])
    if record.get("move_in"):
        parts.append(f"Move-in: {record['move_in']}")
    if record.get("rent"):
        parts.append(f"Rent: {record['rent']}")
    return " | ".join(part for part in parts if part and part != "Unit")




def normalize_rent_value(rent: str) -> str:
    value = str(rent or '').strip()
    if not value:
        return ''
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not m:
        return ''
    return m.group(1).replace(',', '')


def truncate_field(value: str, max_len: int = 180) -> str:
    value = str(value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def build_unit_event_message(event_type: str, record: dict, previous_rent: str = "", current_rent: str = "") -> str:
    title = f"__**{event_type}**__"
    lines = [title]

    unit = truncate_field(record.get("unit", ""), 64)
    beds = truncate_field(record.get("beds", ""))
    baths = truncate_field(record.get("baths", ""))
    sqft = truncate_field(record.get("sqft", ""))
    floor = truncate_field(record.get("floor", ""))
    move_in = truncate_field(record.get("move_in", ""))
    rent = truncate_field(record.get("rent", ""), 80)

    if unit:
        lines.append(f"Unit: {unit}")
    if beds:
        lines.append(f"Beds: {beds}")
    if baths:
        lines.append(f"Baths: {baths}")
    if sqft:
        lines.append(f"Sq. Ft.: {sqft}")
    if floor:
        lines.append(f"Floor: {floor}")
    if move_in:
        lines.append(f"Move-in: {move_in}")

    if event_type == "Price Change":
        prev = truncate_field(previous_rent, 80)
        curr = truncate_field(current_rent, 80)
        if prev:
            lines.append(f"Previous Rent: {prev}")
        if curr:
            lines.append(f"Current Rent: {curr}")
    elif rent:
        lines.append(f"Rent: {rent}")

    lines.append("")
    message = "\n".join(lines)
    if len(message) > DISCORD_MAX_MESSAGE_LEN:
        message = message[: DISCORD_MAX_MESSAGE_LEN - 3].rstrip() + "..."
    return message


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

            print(f"Discord rate-limited for {context}; retrying in {retry_after:.2f}s (attempt {attempt}/{max_attempts}).", file=sys.stderr)
            if attempt == max_attempts:
                print(f"Giving up Discord alert for {context} after repeated 429 responses.", file=sys.stderr)
                return
            time.sleep(max(retry_after, 0.5))
            continue

        if 200 <= resp.status_code < 300:
            return

        print(f"Discord send failed for {context} with HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return


def send_discord_events(
    webhook_url: str,
    property_name: str,
    new_unit_records: List[dict],
    removed_unit_records: List[dict],
    rent_change_events: List[dict],
    url: str,
) -> None:
    messages: List[tuple[str, str]] = []

    for record in new_unit_records:
        messages.append((build_unit_event_message("Addition", record), f"addition {property_name}"))

    for record in removed_unit_records:
        messages.append((build_unit_event_message("Removal", record), f"removal {property_name}"))

    for event in rent_change_events:
        messages.append((
            build_unit_event_message("Price Change", event["record"], event["previous_rent"], event["current_rent"]),
            f"rent-change {property_name}",
        ))

    messages.append((f"URL used for analysis:\n{url}", f"url {property_name}"))

    for message, context in messages:
        send_discord_message(webhook_url, message, context)
        time.sleep(DISCORD_INTER_MESSAGE_DELAY_SECONDS)


def build_heartbeat_message(property_count: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "Apartment monitor heartbeat\n"
        f"Checked: {property_count} properties\n"
        f"Time: {timestamp}\n"
        "No unit changes detected."
    )


def send_heartbeat(webhook_url: str, property_count: int) -> None:
    send_discord_message(webhook_url, build_heartbeat_message(property_count), "heartbeat")


def build_anomaly_warning(property_name: str, url: str) -> str:
    return (
        f"Apartment monitor warning at {property_name}: no units were detected, but prior units existed. "
        "Preserving previous snapshot because this may be a scrape/render failure. "
        f"URL: {url}"
    )


def send_anomaly_warning(webhook_url: str, property_name: str, url: str) -> None:
    send_discord_message(webhook_url, build_anomaly_warning(property_name, url), f"warning {property_name}")


def main() -> int:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set; skipping alerts.", file=sys.stderr)

    properties, heartbeat_enabled = load_config()
    seen_units_by_url = load_seen_units()
    had_unit_changes = False

    for prop in properties:
        name = prop["name"]
        url = prop["url"]
        unit_regex = prop["unit_regex"]

        print(f"Checking {name} ({url})")
        text = extract_visible_text(url)
        current_records = parse_structured_units(text, unit_regex)

        prior_records = seen_units_by_url.get(url, {})
        prior_units = set(prior_records.keys())
        current_units = set(current_records.keys())

        if prior_units and not current_units:
            print(f"No units detected for {name} despite existing snapshot; retrying once after 5 seconds.")
            time.sleep(5)
            retry_text = extract_visible_text(url)
            current_records = parse_structured_units(retry_text, unit_regex)
            current_units = set(current_records.keys())

            if not current_units:
                print(f"Suspicious empty scrape for {name}; preserving previous snapshot.", file=sys.stderr)
                if webhook_url:
                    send_anomaly_warning(webhook_url, name, url)
                continue

        new_units = sorted(current_units - prior_units)
        removed_units = sorted(prior_units - current_units)

        rent_change_events: List[dict] = []
        common_units = sorted(current_units & prior_units)
        for unit in common_units:
            prior_record = prior_records[unit]
            current_record = current_records[unit]
            prior_rent_norm = normalize_rent_value(str(prior_record.get("rent", "")))
            current_rent_norm = normalize_rent_value(str(current_record.get("rent", "")))
            if not prior_rent_norm or not current_rent_norm:
                continue
            if prior_rent_norm != current_rent_norm:
                merged = dict(current_record)
                for key in ["beds", "baths", "sqft", "floor", "move_in"]:
                    if not merged.get(key):
                        merged[key] = prior_record.get(key, "")
                rent_change_events.append({
                    "record": merged,
                    "previous_rent": str(prior_record.get("rent", "")).strip(),
                    "current_rent": str(current_record.get("rent", "")).strip(),
                })

        suspicious_mass_removal = False
        if prior_units:
            removal_ratio = len(removed_units) / len(prior_units)
            suspicious_mass_removal = removal_ratio > SUSPICIOUS_REMOVAL_RATIO

        if suspicious_mass_removal:
            print(f"Suspicious mass removal detected for {name}; preserving previous snapshot.", file=sys.stderr)
            if webhook_url:
                send_anomaly_warning(webhook_url, name, url)
            continue

        if new_units or removed_units or rent_change_events:
            had_unit_changes = True
            new_records_for_alert = [current_records[unit] for unit in new_units]
            removed_records_for_alert = [prior_records[unit] for unit in removed_units]
            if webhook_url:
                send_discord_events(webhook_url, name, new_records_for_alert, removed_records_for_alert, rent_change_events, url)

        seen_units_by_url[url] = current_records

    if heartbeat_enabled and webhook_url and not had_unit_changes:
        send_heartbeat(webhook_url, len(properties))

    save_seen_units(seen_units_by_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
