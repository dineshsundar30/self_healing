import time
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

class SelfHealingListener:
    """
    A Robot Framework Listener that globally patches Appium WebDriver 
    to enable runtime self-healing of locators.
    It uses a rate-limiting cache to ensure we never destroy the performance 
    of aggressive polling loops (e.g. Wait Until Keyword Succeeds).
    """
    
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.webdriver_patched = False
        self.unhealable_cache = {}  # Format: {locator: timestamp}

    def start_keyword(self, data, result):
        self._patch_webdriver()

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return
            
        try:
            from appium.webdriver.webdriver import WebDriver
            
            if not getattr(WebDriver.find_element, '_is_healed', False):
                original_find_element = WebDriver.find_element
                
                # We need a reference to the class scope for the rate limit dictionary
                unhealable_cache = self.unhealable_cache
                
                def healed_find_element(driver_self, by='id', value=None):
                    try:
                        return original_find_element(driver_self, by, value)
                    except Exception as e:
                        current_time = time.time()
                        
                        # If we recently tried and failed to heal this exact locator, 
                        # skip healing to preserve performance during Wait loops.
                        if unhealable_cache.get(value, 0) > current_time:
                            raise e

                        # OK, this is the first (or a significantly delayed) failure for this locator.
                        # We will try self-healing!
                        try:
                            builtin = BuiltIn()
                            builtin.log(f"<b style='color:orange'>WARN: Locator '{value}' failed. Triggering Self-Healing engine...</b>", html=True)
                            
                            # 1. Grab XML context directly from the active device
                            page_source = driver_self.page_source
                            
                            # 2. Heal the locator using logic
                            new_locator = find_healed_locator(value, page_source)
                            
                            if new_locator:
                                builtin.log(f"<b style='color:green'>SUCCESS: Healed locator discovered: '{new_locator}'. Retrying!</b>", html=True)
                                
                                # Healer logic returns pure XPATH string format
                                from selenium.webdriver.common.by import By
                                new_by = By.XPATH
                                new_value = new_locator
                                if new_locator.startswith('xpath='):
                                    new_value = new_locator.replace('xpath=', '', 1)
                                
                                try:
                                    # Attempt the new fixed locator!
                                    return original_find_element(driver_self, by=new_by, value=new_value)
                                except Exception as inner_e:
                                    # Even the fixed locator failed? Unhealable.
                                    unhealable_cache[value] = current_time + 10.0 # Ignore for 10 seconds
                                    raise e
                            else:
                                # Healer logic found nothing similar.
                                unhealable_cache[value] = current_time + 10.0 # Ignore for 10 seconds
                                raise e # Bubble up original error
                                
                        except Exception:
                            # Any failure during the healing pipeline, mark unhealable and bubble original error
                            unhealable_cache[value] = current_time + 10.0
                            raise e
                
                healed_find_element._is_healed = True
                WebDriver.find_element = healed_find_element
                self.webdriver_patched = True
                
                try:
                    BuiltIn().log("Self-Healing Engine activated globally via Webdriver Patch.", "INFO")
                except:
                    pass
        except Exception:
            pass
