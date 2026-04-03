import xml.etree.ElementTree as ET
import json
import os
import re

HEALED_LOCATORS_FILE = "healed_locators.json"

def find_healed_locator(old_locator, page_source_xml):
    """
    Heuristic logic to heal a broken locator, supporting implicit bare XPath.
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
    except ET.ParseError:
        return None

    # 3. Heuristic Matching
    cleaned_value = re.sub(r'[^a-zA-Z0-9-]', ' ', value)
    keywords = [kw.lower() for kw in cleaned_value.split() if len(kw) > 3]

    if not keywords:
        return None

    for node in root.iter():
        attribs = node.attrib
        text = str(attribs.get('text', '')).lower()
        content_desc = str(attribs.get('content-desc', '')).lower()
        resource_id = str(attribs.get('resource-id', '')).lower()

        # Look for matching keywords in visible text, descriptions, or resource-id
        for kw in keywords:
            if kw in text or kw in content_desc or kw in resource_id:
                new_appium_id = attribs.get('resource-id')
                new_text = attribs.get('text')
                
                # Construct new locator in XPath format as requested
                new_locator = None
                if new_appium_id:
                    new_locator = f"//*[@resource-id='{new_appium_id}']"
                elif new_text:
                    new_locator = f"//*[@text='{new_text}']"

                if new_locator:
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
