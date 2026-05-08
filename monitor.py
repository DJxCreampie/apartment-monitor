import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urljoin

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
RENT_PATTERN = re.compile(r"\$[\d,]+")


def load_config() -> Tuple[List[dict], bool]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    properties = data.get("properties", [])
    cleaned = []
    for item in properties:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("url")
        unit_regex = item.get("unit_regex") or DEFAULT_UNIT_REGEX
        parser = (item.get("parser") or "maa").strip().lower()
        if name and url:
            cleaned.append({"name": name, "url": url, "unit_regex": unit_regex, "parser": parser})

    return cleaned, bool(data.get("send_heartbeat", False))


def normalize_unit_candidate(candidate: str) -> str | None:
    value = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9-]+$", "", str(candidate).strip())
    if not value or value.lower() in FALSE_POSITIVES or not re.search(r"\d", value):
        return None
    if not re.match(r"^\d[A-Za-z0-9-]{0,7}$", value):
        return None
    return value.upper()


def normalize_unit_record(unit: str, rent: str = "") -> dict:
    return {"unit": unit, "rent": rent}


def load_seen_units() -> Dict[str, Dict[str, dict]]:
    if not SEEN_PATH.exists():
        return {}
    data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}

    out: Dict[str, Dict[str, dict]] = {}
    for url, value in data.items():
        units: Dict[str, dict] = {}
        if isinstance(value, list):
            for unit in value:
                uid = normalize_unit_candidate(str(unit))
                if uid:
                    units[uid] = normalize_unit_record(uid)
        elif isinstance(value, dict):
            for key, obj in value.items():
                uid = normalize_unit_candidate(str(key))
                if not uid:
                    continue
                rent = ""
                if isinstance(obj, dict):
                    unit_field = normalize_unit_candidate(str(obj.get("unit", uid))) or uid
                    rent_match = RENT_PATTERN.search(str(obj.get("rent", "")))
                    rent = rent_match.group(0) if rent_match else ""
                    uid = unit_field
                units[uid] = normalize_unit_record(uid, rent)
        out[url] = units
    return out


def save_seen_units(data: Dict[str, Dict[str, dict]]) -> None:
    output = {url: {u: data[url][u] for u in sorted(data[url])} for url in sorted(data)}
    SEEN_PATH.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


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
            return page.inner_text("body") or ""
        finally:
            browser.close()


def parse_maa_units(url: str, unit_regex: str) -> Dict[str, dict]:
    text = extract_visible_text(url)
    return parse_structured_units(text, unit_regex)


def parse_entrata_units(url: str) -> Dict[str, dict]:
    units: Dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2500)

            detail_links = set()
            for href in page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))") or []:
                if not href:
                    continue
                if "/floorplans/" in href.lower():
                    detail_links.add(urljoin(url, href))

            for detail_url in sorted(detail_links):
                detail = browser.new_page()
                try:
                    detail.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        detail.wait_for_load_state("networkidle", timeout=8_000)
                    except PlaywrightTimeoutError:
                        pass
                    detail.wait_for_timeout(1200)

                    for txt in detail.eval_on_selector_all("body *", "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)") or []:
                        line = re.sub(r"\s+", " ", txt).strip()
                        if not line:
                            continue
                        unit_match = re.search(r"\b(?:Unit|Apt|Apartment)\s*#?\s*([A-Za-z0-9-]{1,10})\b", line, re.IGNORECASE)
                        if not unit_match:
                            continue
                        uid = normalize_unit_candidate(unit_match.group(1))
                        if not uid:
                            continue

                        unit_rent = ""
                        for amt in re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", line):
                            n = normalize_rent_value(amt)
                            if n and int(float(n)) >= 1000:
                                unit_rent = f"${int(float(n)):,}"
                                break
                        if not unit_rent:
                            continue
                        units[uid] = normalize_unit_record(uid, unit_rent)
                finally:
                    detail.close()
        finally:
            browser.close()
    return units


def detect_units(page_text: str, unit_regex: str) -> Set[str]:
    matches = re.compile(unit_regex, re.IGNORECASE).findall(page_text)
    units = set()
    for match in matches:
        candidate = match[0] if isinstance(match, tuple) else match
        uid = normalize_unit_candidate(candidate)
        if uid:
            units.add(uid)
    return units


def parse_structured_units(page_text: str, unit_regex: str) -> Dict[str, dict]:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in page_text.splitlines() if ln.strip()]
    units: Dict[str, dict] = {}

    i = 0
    while i < len(lines):
        if lines[i].lower() == "unit" and i + 1 < len(lines):
            uid = normalize_unit_candidate(lines[i + 1])
            if uid:
                rec = normalize_unit_record(uid)
                block_start = i + 2
                block_end = min(len(lines), block_start + 20)
                j = block_start
                while j < len(lines) and j < block_end:
                    low = lines[j].lower()
                    if low == "unit" or low == "additional unit features":
                        break
                    j += 1
                block_end = j

                for k in range(block_start, block_end):
                    if lines[k].lower() == "monthly":
                        for m in range(k + 1, block_end):
                            r = RENT_PATTERN.search(lines[m])
                            if r:
                                rec["rent"] = r.group(0)
                                break
                        break
                if not rec["rent"]:
                    for k in range(block_start, block_end):
                        r = RENT_PATTERN.search(lines[k])
                        if r:
                            rec["rent"] = r.group(0)
                            break

                units[uid] = rec
                i = block_end
                continue
        i += 1

    for uid in detect_units(page_text, unit_regex):
        units.setdefault(uid, normalize_unit_record(uid))
    return units


def normalize_rent_value(rent: str) -> str:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", str(rent or "").strip())
    return m.group(1).replace(",", "") if m else ""


def truncate_field(value: str, max_len: int = 180) -> str:
    value = str(value or "").strip()
    return value if len(value) <= max_len else value[: max_len - 3].rstrip() + "..."


def build_unit_event_message(event_type: str, record: dict, previous_rent: str = "", current_rent: str = "") -> str:
    lines = [f"__**{event_type}**__"]
    unit = truncate_field(record.get("unit", ""), 64)
    rent = truncate_field(record.get("rent", ""), 80)

    if unit:
        lines.append(f"Unit: {unit}")
    if event_type == "Price Change":
        pr = truncate_field(previous_rent, 80)
        cr = truncate_field(current_rent, 80)
        if pr:
            lines.append(f"Previous Rent: {pr}")
        if cr:
            lines.append(f"Current Rent: {cr}")
    elif rent:
        lines.append(f"Rent: {rent}")

    lines.append("")
    msg = "\n".join(lines)
    return msg[: DISCORD_MAX_MESSAGE_LEN - 3] + "..." if len(msg) > DISCORD_MAX_MESSAGE_LEN else msg


def send_discord_message(webhook_url: str, message: str, context: str) -> None:
    for attempt in range(1, 5):
        try:
            resp = requests.post(webhook_url, json={"content": message}, timeout=20)
        except requests.RequestException as exc:
            print(f"Discord send failed for {context} (attempt {attempt}/4): {exc}", file=sys.stderr)
            if attempt == 4:
                return
            time.sleep(min(2**attempt, 10))
            continue
        if resp.status_code == 429:
            retry_after = 2.0
            try:
                retry_after = float(resp.json().get("retry_after", retry_after))
                if retry_after > 100:
                    retry_after /= 1000.0
            except Exception:
                pass
            if attempt == 4:
                return
            time.sleep(max(retry_after, 0.5))
            continue
        if 200 <= resp.status_code < 300:
            return
        return


def send_discord_events(webhook_url: str, property_name: str, new_records: List[dict], removed_records: List[dict], rent_events: List[dict], url: str) -> None:
    queue: List[tuple[str, str]] = []
    for r in new_records:
        queue.append((build_unit_event_message("Addition", r), f"addition {property_name}"))
    for r in removed_records:
        queue.append((build_unit_event_message("Removal", r), f"removal {property_name}"))
    for e in rent_events:
        queue.append((build_unit_event_message("Price Change", e["record"], e["previous_rent"], e["current_rent"]), f"price-change {property_name}"))
    queue.append((f"URL used for analysis:\n{url}", f"url {property_name}"))

    for msg, ctx in queue:
        send_discord_message(webhook_url, msg, ctx)
        time.sleep(DISCORD_INTER_MESSAGE_DELAY_SECONDS)


def build_heartbeat_message(property_count: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Apartment monitor heartbeat\nChecked: {property_count} properties\nTime: {ts}\nNo unit changes detected."


def send_anomaly_warning(webhook_url: str, property_name: str, url: str) -> None:
    msg = f"Apartment monitor warning at {property_name}: no units were detected, but prior units existed. Preserving previous snapshot because this may be a scrape/render failure. URL: {url}"
    send_discord_message(webhook_url, msg, f"warning {property_name}")


def main() -> int:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    properties, heartbeat_enabled = load_config()
    seen = load_seen_units()
    had_changes = False

    for prop in properties:
        name, url, unit_regex, parser_name = prop["name"], prop["url"], prop["unit_regex"], prop["parser"]
        print(f"Checking {name} using parser {parser_name}")
        if parser_name == "entrata":
            current = parse_entrata_units(url)
        else:
            current = parse_maa_units(url, unit_regex)
        prior = seen.get(url, {})
        prior_units, current_units = set(prior), set(current)

        if prior_units and not current_units:
            time.sleep(5)
            if parser_name == "entrata":
                current = parse_entrata_units(url)
            else:
                current = parse_maa_units(url, unit_regex)
            current_units = set(current)
            if not current_units:
                if webhook_url:
                    send_anomaly_warning(webhook_url, name, url)
                continue

        new_units = sorted(current_units - prior_units)
        removed_units = sorted(prior_units - current_units)

        rent_events: List[dict] = []
        for u in sorted(current_units & prior_units):
            pr = normalize_rent_value(prior[u].get("rent", ""))
            cr = normalize_rent_value(current[u].get("rent", ""))
            if pr and cr and pr != cr:
                rent_events.append({"record": current[u], "previous_rent": prior[u].get("rent", ""), "current_rent": current[u].get("rent", "")})

        if prior_units and (len(removed_units) / len(prior_units)) > SUSPICIOUS_REMOVAL_RATIO:
            if webhook_url:
                send_anomaly_warning(webhook_url, name, url)
            continue

        if new_units or removed_units or rent_events:
            had_changes = True
            if webhook_url:
                send_discord_events(webhook_url, name, [current[u] for u in new_units], [prior[u] for u in removed_units], rent_events, url)

        seen[url] = current

    if heartbeat_enabled and webhook_url and not had_changes:
        send_discord_message(webhook_url, build_heartbeat_message(len(properties)), "heartbeat")

    save_seen_units(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
