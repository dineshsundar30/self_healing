import xml.etree.ElementTree as ET
import json
import os
import re

HEALED_LOCATORS_FILE = "healed_locators.json"

def find_healed_locator(old_locator, page_source_xml):
    """
    Heuristic logic to heal a broken locator. Extremely strict matching 
    to avoid false positives on negative assertions.
    """
    # 1. Check if we already healed this locator in a previous run
    if os.path.exists(HEALED_LOCATORS_FILE):
        with open(HEALED_LOCATORS_FILE, "r") as f:
            try:
                cache = json.load(f)
                if old_locator in cache:
                    return cache[old_locator]
            except:
                pass

    # Extract some intent from the old locator
    value = old_locator
    if str(old_locator).startswith('//'):
        value = old_locator
    elif "=" in str(old_locator):
        parts = old_locator.split("=", 1)
        if parts[0].strip().lower() in ['id', 'xpath', 'name', 'text', 'class', 'accessibility_id']:
            value = parts[1]

    if not value:
        return None

    # 2. Parse the current App UI state (XML)
    try:
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except (ET.ParseError, TypeError):
        return None

    # 3. Heuristic Matching (STRICT)
    # Extract only the specific part of the locator (e.g., after ':id/' or '/')
    specific_part = value
    if ':id/' in value:
        specific_part = value.split(':id/')[-1]
    elif '/' in value:
        specific_part = value.split('/')[-1]

    cleaned_value = re.sub(r'[^a-zA-Z0-9-]', ' ', specific_part)
    
    GENERIC_WORDS = {'android', 'systemui', 'com', 'renault', 'mydriving', 'layout', 'widget', 'view', 'text', 'button', 'image', 'car', 'id'}
    # Only keep keywords that are highly specific (>= 5 characters) and not generic
    keywords = [kw.lower() for kw in cleaned_value.split() if len(kw) >= 5 and kw.lower() not in GENERIC_WORDS]

    if not keywords:
        return None

    for node in root.iter():
        attribs = node.attrib
        text = str(attribs.get('text', '')).lower()
        content_desc = str(attribs.get('content-desc', '')).lower()
        resource_id = str(attribs.get('resource-id', '')).lower()

        # Isolate the specific part of the candidate resource_id to compare fairly
        candidate_specific_id = resource_id
        if ':id/' in resource_id:
            candidate_specific_id = resource_id.split(':id/')[-1]

        # Look for matching keywords in visible text, descriptions, or specific ID part
        for kw in keywords:
            if kw in text or kw in content_desc or kw in candidate_specific_id:
                new_appium_id = attribs.get('resource-id')
                new_text = attribs.get('text')
                
                new_locator = None
                if new_appium_id:
                    new_locator = f"//*[@resource-id='{new_appium_id}']"
                elif new_text:
                    new_locator = f"//*[@text='{new_text}']"

                # If we construct the EXACT same locator, it's not actually healed.
                if new_locator and new_locator != old_locator and value not in new_locator:
                    # 4. Save to cache
                    save_healed_locator(old_locator, new_locator)
                    return new_locator

    return None

def save_healed_locator(old, new):
    """Persists the healed locator so subsequent executions don't need to heal again."""
    cache = {}
    if os.path.exists(HEALED_LOCATORS_FILE):
        with open(HEALED_LOCATORS_FILE, "r") as f:
            try:
                cache = json.load(f)
            except:
                pass
    
    cache[old] = new
    with open(HEALED_LOCATORS_FILE, "w") as f:
        json.dump(cache, f, indent=4)
