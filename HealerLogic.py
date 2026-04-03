"""
HealerLogic.py
==============
Pure heuristic logic for healing broken Appium locators.

Algorithm
---------
1. Check disk cache (healed_locators.json) — instant return if seen before.
2. Extract the "meaningful" part of the locator (strip package prefixes).
3. Build a keyword list: tokens >= 4 chars that are not in a generic-word
   blocklist.  Min-length reduced to 4 (was 5) to catch words like "icon",
   "info", "trip", etc. that frequently appear in resource-ids.
4. Walk every node of the live XML page source.  For each node, score it by
   how many keywords appear in its resource-id, text, or content-desc.
5. Collect ALL matches, pick the one with the highest score.
   Ties broken by preferring resource-id locators over text locators.
6. Return the winning XPath or None if nothing matched.

All decisions are logged so the caller (SelfHealingListener) can write them
to the Robot report.
"""

import xml.etree.ElementTree as ET
import json
import os
import re
from typing import Optional, List, Tuple

HEALED_LOCATORS_FILE = "healed_locators.json"

# Words too common in Android / project IDs to be discriminating
_GENERIC_WORDS = {
    'android', 'systemui', 'renault', 'mydriving', 'driving',
    'layout', 'widget', 'frame', 'linear', 'relative', 'scroll',
    'recycler', 'coordinator', 'constraint', 'include',
    'view', 'text', 'button', 'image', 'icon',
    'car', 'com', 'id', 'main', 'root', 'page', 'item', 'list',
    'container', 'content', 'panel', 'group', 'holder',
}

# Minimum keyword length (inclusive). Lowered to 4 to catch short but
# meaningful tokens like "trip", "info", "fuel", "dash", "safe".
_MIN_KW_LEN = 4


def find_healed_locator(old_locator: str, page_source_xml: str) -> Optional[str]:
    """
    Main entry point.  Returns a healed XPath string, or None.

    Parameters
    ----------
    old_locator     : the broken locator string as passed to find_element
    page_source_xml : raw XML string from driver.page_source
    """
    if not old_locator or not isinstance(old_locator, str):
        return None
    if not page_source_xml or not isinstance(page_source_xml, str):
        return None

    # ── 1. Disk cache ─────────────────────────────────────────────────────────
    cached = _lookup_cache(old_locator)
    if cached:
        return cached

    # ── 2. Extract meaningful search value ───────────────────────────────────
    search_value = _extract_search_value(old_locator)
    if not search_value:
        return None

    # ── 3. Build keyword list ─────────────────────────────────────────────────
    keywords = _build_keywords(search_value)
    if not keywords:
        return None

    # ── 4 & 5. Parse XML and score candidates ────────────────────────────────
    try:
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except (ET.ParseError, TypeError, UnicodeEncodeError, ValueError):
        return None

    candidates: List[Tuple[int, int, str]] = []  # (score, priority, xpath)

    for node in root.iter():
        attribs      = node.attrib
        raw_id       = attribs.get('resource-id', '')
        raw_text     = attribs.get('text', '')
        raw_desc     = attribs.get('content-desc', '')
        raw_class    = attribs.get('class', '')

        # Normalise for matching
        norm_id   = raw_id.lower()
        norm_text = raw_text.lower()
        norm_desc = raw_desc.lower()

        # Isolate the local part of the resource-id (after ':id/')
        local_id = norm_id
        if ':id/' in norm_id:
            local_id = norm_id.split(':id/')[-1]

        score = sum(
            1 for kw in keywords
            if kw in local_id or kw in norm_text or kw in norm_desc
        )

        if score == 0:
            continue

        # Build the candidate XPath — prefer resource-id (stable), then
        # content-desc (accessibility), then visible text (fragile).
        if raw_id:
            xpath      = f"//*[@resource-id='{raw_id}']"
            priority   = 0          # highest priority
        elif raw_desc:
            xpath      = f"//*[@content-desc='{raw_desc}']"
            priority   = 1
        elif raw_text:
            xpath      = f"//*[@text='{raw_text}']"
            priority   = 2
        else:
            continue                # no usable attribute

        # Never return the exact same locator we were given
        if xpath == old_locator:
            continue

        # Avoid locators that still embed the old broken value
        if search_value in xpath:
            continue

        candidates.append((score, priority, xpath))

    if not candidates:
        return None

    # Sort: highest score first, then lowest priority number (id > desc > text)
    candidates.sort(key=lambda x: (-x[0], x[1]))
    best_xpath = candidates[0][2]

    _save_cache(old_locator, best_xpath)
    return best_xpath


# ── Private helpers ──────────────────────────────────────────────────────────

def _extract_search_value(locator: str) -> str:
    """
    Pull the meaningful search string out of any locator format:
      - Raw XPath string starting with //  → returned as-is
      - 'id=com.example:id/foo'            → 'com.example:id/foo'
      - 'xpath=//*[@resource-id="foo"]'   → '//*[@resource-id="foo"]'
      - anything else                      → returned as-is
    """
    if locator.startswith('//') or locator.startswith('//*'):
        return locator

    if '=' in locator:
        prefix, _, rest = locator.partition('=')
        if prefix.strip().lower() in {
            'id', 'xpath', 'name', 'text', 'class', 'accessibility_id',
            'css', 'link text', 'partial link text', 'tag name',
        }:
            return rest.strip()

    return locator.strip()


def _build_keywords(search_value: str) -> List[str]:
    """
    Tokenise the search value into discriminating keywords.

    Strategy:
    - If it's an XPath, pull out the attribute value(s) from quotes.
    - Otherwise, isolate the local part (after ':id/' or last '/').
    - Split on non-alphanumeric characters.
    - Filter out short and generic tokens.
    """
    # If it looks like an XPath, extract quoted values
    if search_value.startswith('//') or search_value.startswith('//*'):
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", search_value)
        raw_parts = ' '.join(quoted) if quoted else search_value
    else:
        raw_parts = search_value

    # Isolate local portion of resource-id
    specific = raw_parts
    if ':id/' in raw_parts:
        specific = raw_parts.split(':id/')[-1]
    elif '/' in raw_parts and not raw_parts.startswith('//'):
        specific = raw_parts.split('/')[-1]

    tokens = re.sub(r'[^a-zA-Z0-9]', ' ', specific).split()
    keywords = [
        t.lower() for t in tokens
        if len(t) >= _MIN_KW_LEN and t.lower() not in _GENERIC_WORDS
    ]
    return list(dict.fromkeys(keywords))   # deduplicate, preserve order


def _lookup_cache(old_locator: str) -> Optional[str]:
    if not os.path.exists(HEALED_LOCATORS_FILE):
        return None
    try:
        with open(HEALED_LOCATORS_FILE, 'r') as fh:
            cache = json.load(fh)
        return cache.get(old_locator)
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _save_cache(old: str, new: str) -> None:
    cache: dict = {}
    if os.path.exists(HEALED_LOCATORS_FILE):
        try:
            with open(HEALED_LOCATORS_FILE, 'r') as fh:
                cache = json.load(fh)
        except (json.JSONDecodeError, OSError):
            cache = {}
    cache[old] = new
    try:
        with open(HEALED_LOCATORS_FILE, 'w') as fh:
            json.dump(cache, fh, indent=4)
    except OSError:
        pass
