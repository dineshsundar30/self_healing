"""
HealerLogic.py
==============
Pure heuristic logic for healing broken Appium locators.

Algorithm
---------
1. Check disk cache (healed_locators.json) — instant return if seen before.
2. Extract the "meaningful" part of the locator (strip package prefixes).
3. Build keyword list — camelCase is split FIRST so 'safetyScoreText' becomes
   ['safety', 'score'] not the useless single token 'safetyscoretext'.
4. Walk every node of the live XML page source, score by keyword matches.
5. Pick the highest-scoring candidate (prefer resource-id > content-desc > text).
6. Return the winning XPath or None.
"""

import xml.etree.ElementTree as ET
import json
import os
import re
from typing import Optional, List, Tuple

HEALED_LOCATORS_FILE = "healed_locators.json"

# Words too common / structural to be discriminating in resource-ids
_GENERIC_WORDS = {
    'android', 'systemui', 'renault', 'mydriving', 'driving',
    'layout', 'widget', 'frame', 'linear', 'relative', 'scroll',
    'recycler', 'coordinator', 'constraint', 'include',
    'view', 'button', 'image',
    'car', 'com', 'id', 'main', 'root', 'page', 'item', 'list',
    'container', 'content', 'panel', 'group', 'holder',
}

# IMPORTANT: 'text', 'icon', 'info', 'score', 'safety', 'trip', 'fuel',
# 'dash', 'eco', 'energy' are NOT in the generic list — they ARE meaningful
# discriminators for this project's resource-ids.

_MIN_KW_LEN = 3   # lowered to 3 to catch 'eco', 'nav', 'bar'


def find_healed_locator(old_locator: str, page_source_xml: str) -> Optional[str]:
    if not old_locator or not isinstance(old_locator, str):
        return None
    if not page_source_xml or not isinstance(page_source_xml, str):
        return None

    # ── 1. Cache lookup ───────────────────────────────────────────────────────
    cached = _lookup_cache(old_locator)
    if cached:
        return cached

    # ── 2. Extract search value ───────────────────────────────────────────────
    search_value = _extract_search_value(old_locator)
    if not search_value:
        return None

    # ── 3. Build keyword list (camelCase-aware) ───────────────────────────────
    keywords = _build_keywords(search_value)
    if not keywords:
        return None

    # ── 4. Parse XML ──────────────────────────────────────────────────────────
    try:
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except (ET.ParseError, TypeError, UnicodeEncodeError, ValueError):
        return None

    # ── 5. Score every node ───────────────────────────────────────────────────
    candidates: List[Tuple[int, int, str]] = []   # (score, priority, xpath)

    for node in root.iter():
        attribs   = node.attrib
        raw_id    = attribs.get('resource-id', '')
        raw_text  = attribs.get('text', '')
        raw_desc  = attribs.get('content-desc', '')

        norm_id   = raw_id.lower()
        norm_text = raw_text.lower()
        norm_desc = raw_desc.lower()

        # Isolate local part of resource-id (after ':id/')
        local_id = norm_id.split(':id/')[-1] if ':id/' in norm_id else norm_id

        score = sum(
            1 for kw in keywords
            if kw in local_id or kw in norm_text or kw in norm_desc
        )
        if score == 0:
            continue

        # Build candidate XPath
        if raw_id:
            xpath, priority = f"//*[@resource-id='{raw_id}']", 0
        elif raw_desc:
            xpath, priority = f"//*[@content-desc='{raw_desc}']", 1
        elif raw_text:
            xpath, priority = f"//*[@text='{raw_text}']", 2
        else:
            continue

        if xpath == old_locator:
            continue

        # Skip candidates whose local-id part contains the old broken local-id
        # (would return an equally broken locator)
        old_local = search_value.split(':id/')[-1] if ':id/' in search_value else ''
        if old_local and old_local.lower() in xpath.lower():
            continue

        candidates.append((score, priority, xpath))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[0], x[1]))
    best_xpath = candidates[0][2]

    _save_cache(old_locator, best_xpath)
    return best_xpath


# ── Private helpers ───────────────────────────────────────────────────────────

def _split_camel(s: str) -> List[str]:
    """
    Split camelCase / PascalCase into individual words.
    'safetyScoreText' → ['safety', 'Score', 'Text']
    'myDrivingDashboardInfo' → ['my', 'Driving', 'Dashboard', 'Info']
    """
    return re.sub(r'([A-Z])', r' \1', s).split()


def _extract_search_value(locator: str) -> str:
    """Extract the meaningful string from any locator format."""
    if locator.startswith('//'):
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
    Build a list of discriminating keywords from a locator string.

    Key steps:
    1. If XPath, pull quoted attribute values first.
    2. Strip the package prefix (everything before ':id/').
    3. SPLIT CAMELCASE — 'safetyScoreText' → ['safety', 'score', 'text']
    4. Also split on underscores/hyphens for snake_case IDs.
    5. Filter: length >= _MIN_KW_LEN and not in generic-words list.
    """
    # Step 1 — extract quoted values from XPath
    if search_value.startswith('//'):
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", search_value)
        raw = ' '.join(quoted) if quoted else search_value
    else:
        raw = search_value

    # Step 2 — isolate local part of resource-id
    if ':id/' in raw:
        raw = raw.split(':id/')[-1]
    elif '/' in raw and not raw.startswith('//'):
        raw = raw.split('/')[-1]

    # Step 3 — split camelCase FIRST (before lowercasing)
    camel_parts = _split_camel(raw)

    # Step 4 — further split each part on non-alphanumeric (underscores, hyphens)
    all_tokens: List[str] = []
    for part in camel_parts:
        sub = re.sub(r'[^a-zA-Z0-9]', ' ', part).split()
        all_tokens.extend(sub)

    # Step 5 — filter
    keywords = [
        t.lower() for t in all_tokens
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
    except (json.JSONDecodeError, OSError):
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
