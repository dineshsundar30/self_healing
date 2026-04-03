import xml.etree.ElementTree as ET
import json
import os
import re

HEALED_LOCATORS_FILE = "healed_locators.json"


def find_healed_locator(old_locator: str, page_source_xml: str):
    """
    Heuristic logic to heal a broken locator.

    Returns a new XPath string on success, or None if no safe match found.
    Strict keyword matching is used to avoid false positives.
    """
    if not old_locator or not isinstance(old_locator, str):
        return None
    if not page_source_xml or not isinstance(page_source_xml, str):
        return None

    # ── 1. Cache lookup ──────────────────────────────────────────────────────
    if os.path.exists(HEALED_LOCATORS_FILE):
        with open(HEALED_LOCATORS_FILE, "r") as f:
            try:
                cache = json.load(f)
                if old_locator in cache:
                    return cache[old_locator]
            except (json.JSONDecodeError, OSError):
                pass

    # ── 2. Extract the meaningful value from the locator ────────────────────
    value = old_locator
    if not old_locator.startswith('//'):
        if "=" in old_locator:
            prefix, _, rest = old_locator.partition("=")
            if prefix.strip().lower() in {
                'id', 'xpath', 'name', 'text', 'class', 'accessibility_id'
            }:
                value = rest

    if not value:
        return None

    # ── 3. Parse the live XML page source ────────────────────────────────────
    try:
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except (ET.ParseError, TypeError, UnicodeEncodeError):
        return None

    # ── 4. Build keyword list from the specific (non-package) part ──────────
    specific_part = value
    if ':id/' in value:
        specific_part = value.split(':id/')[-1]
    elif '/' in value and not value.startswith('//'):
        specific_part = value.split('/')[-1]

    cleaned = re.sub(r'[^a-zA-Z0-9-]', ' ', specific_part)

    GENERIC_WORDS = {
        'android', 'systemui', 'com', 'renault', 'mydriving',
        'layout', 'widget', 'view', 'text', 'button', 'image', 'car', 'id'
    }
    keywords = [
        kw.lower()
        for kw in cleaned.split()
        if len(kw) >= 5 and kw.lower() not in GENERIC_WORDS
    ]

    if not keywords:
        return None

    # ── 5. Walk the XML tree looking for a single unambiguous match ──────────
    candidates = []

    for node in root.iter():
        attribs = node.attrib
        text           = str(attribs.get('text', '')).lower()
        content_desc   = str(attribs.get('content-desc', '')).lower()
        resource_id    = str(attribs.get('resource-id', '')).lower()

        candidate_id = resource_id
        if ':id/' in resource_id:
            candidate_id = resource_id.split(':id/')[-1]

        matched_kws = [
            kw for kw in keywords
            if kw in text or kw in content_desc or kw in candidate_id
        ]

        if not matched_kws:
            continue

        # Build candidate locator
        raw_id   = attribs.get('resource-id')
        raw_text = attribs.get('text')

        new_locator = None
        if raw_id:
            new_locator = f"//*[@resource-id='{raw_id}']"
        elif raw_text:
            new_locator = f"//*[@text='{raw_text}']"

        if not new_locator:
            continue

        # Skip if it resolves to the same locator (not actually healed)
        if new_locator == old_locator:
            continue

        # Skip if the original broken value still appears inside the new one
        # (prevents returning a superficially different but equivalent locator)
        if value in new_locator:
            continue

        candidates.append((len(matched_kws), new_locator))

    if not candidates:
        return None

    # Pick the candidate that matched the most keywords (most specific)
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_locator = candidates[0][1]

    # ── 6. Persist and return ────────────────────────────────────────────────
    _save_healed_locator(old_locator, best_locator)
    return best_locator


def _save_healed_locator(old: str, new: str) -> None:
    """Persists a healed locator mapping to disk for re-use across runs."""
    cache = {}
    if os.path.exists(HEALED_LOCATORS_FILE):
        with open(HEALED_LOCATORS_FILE, "r") as f:
            try:
                cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    cache[old] = new

    with open(HEALED_LOCATORS_FILE, "w") as f:
        json.dump(cache, f, indent=4)
