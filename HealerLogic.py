import xml.etree.ElementTree as ET
import json
import os

HEALED_LOCATORS_FILE = "healed_locators.json"

def find_healed_locator(old_locator, page_source_xml):
    """
    Basic heuristic logic to heal a broken locator.
    In a real system, you could replace this with a local LLM call or Computer Vision.
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
    if "=" in old_locator:
        strategy, value = old_locator.split("=", 1)
    else:
        return None  # Unsupported structure for this POC

    # 2. Parse the current App UI state (XML)
    try:
        # Appium Page Source is usually XML
        root = ET.fromstring(page_source_xml.encode('utf-8'))
    except ET.ParseError:
        return None

    # 3. Heuristic Matching
    # Break down the old locator to find meaningful keywords. 
    # e.g., 'login_button' -> ['login', 'button']
    keywords = value.lower().replace('_', ' ').replace('-', ' ').replace('/', ' ').split()

    for node in root.iter():
        attribs = node.attrib
        text = str(attribs.get('text', '')).lower()
        content_desc = str(attribs.get('content-desc', '')).lower()
        resource_id = str(attribs.get('resource-id', '')).lower()
        class_name = str(attribs.get('class', ''))

        # Look for matching keywords in visible text or descriptions
        for kw in keywords:
            if len(kw) > 3 and (kw in text or kw in content_desc):
                # We found an element that looks very similar based on text intent!
                new_appium_id = attribs.get('resource-id')
                new_text = attribs.get('text')
                
                # Construct new locator
                new_locator = None
                if new_appium_id:
                    new_locator = f"id={new_appium_id}"
                elif new_text:
                    new_locator = f"xpath=//*{class_name}[@text='{new_text}']" if class_name else f"xpath=//*[@text='{new_text}']"

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
