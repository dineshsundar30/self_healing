"""
HealerLogic.py
Pure heuristic logic for healing broken Appium locators.
"""
import xml.etree.ElementTree as ET
import json
import os
import re
from typing import Optional, List

HEALED_LOCATORS_FILE = "healed_locators.json"

# Generic words to ignore to prevent false matches in IVI systems
_GENERIC_WORDS = {
    'android', 'systemui', 'layout', 'widget', 'frame', 'linear', 
    'relative', 'view', 'button', 'image', 'container', 'content'
}

def find_healed_locator(old_locator: str, page_source_xml: str) -> Optional[str]:
    """Scans page source for a candidate that matches the 'intent' of the old locator."""
    # 1. Check cache first
    cached = _lookup_cache(old_locator)
    if cached:
        return cached

    # 2. Extract keywords (e.g., 'btn_music_play' -> ['music', 'play'])
    # Splits camelCase and snake_case
    clean_val = old_locator.split('/')[-1].split(':id/')[-1]
    tokens = re.sub(r'([A-Z])', r' \1', clean_val).replace('_', ' ').split()
    keywords = [t.lower() for t in tokens if len(t) >= 3 and t.lower() not in _GENERIC_WORDS]

    if not keywords:
        return None

    try:
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except Exception:
        return None

    best_xpath = None
    highest_score = 0

    # 3. Iterate through all elements in the IVI UI tree
    for node in root.iter():
        attr_str = " ".join(node.attrib.values()).lower()
        score = sum(1 for kw in keywords if kw in attr_str)

        if score > highest_score:
            # Prioritize resource-id for stability
            res_id = node.attrib.get('resource-id')
            if res_id:
                highest_score = score
                best_xpath = f"//*[@resource-id='{res_id}']"
            elif node.attrib.get('content-desc'):
                highest_score = score
                best_xpath = f"//*[@content-desc='{node.attrib.get('content-desc')}']"

    # 4. Save to cache if we found a strong match
    if best_xpath and highest_score >= (len(keywords) / 2):
        _save_cache(old_locator, best_xpath)
        return best_xpath

    return None

def _lookup_cache(old_locator: str) -> Optional[str]:
    if os.path.exists(HEALED_LOCATORS_FILE):
        try:
            with open(HEALED_LOCATORS_FILE, 'r') as f:
                return json.load(f).get(old_locator)
        except: pass
    return None

def _save_cache(old: str, new: str):
    cache = {}
    if os.path.exists(HEALED_LOCATORS_FILE):
        try:
            with open(HEALED_LOCATORS_FILE, 'r') as f:
                cache = json.load(f)
        except: pass
    cache[old] = new
    with open(HEALED_LOCATORS_FILE, 'w') as f:
        json.dump(cache, f, indent=4)
