import traceback
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

class SelfHealingListener:
    """
    A Robot Framework Listener that globally patches Appium WebDriver 
    and AppiumLibrary to enable runtime self-healing of locators.
    """
    
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.webdriver_patched = False
        self.appiumlib_patched = False

    def start_keyword(self, data, result):
        self._patch_webdriver()
        self._patch_appium_library()

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return
            
        try:
            from appium.webdriver.webdriver import WebDriver
            
            if not getattr(WebDriver.find_element, '_is_healed', False):
                original_find_element = WebDriver.find_element
                
                def healed_find_element(driver_self, by='id', value=None):
                    try:
                        return original_find_element(driver_self, by, value)
                    except Exception as e:
                        try:
                            builtin = BuiltIn()
                            builtin.log(f"<b style='color:orange'>WARN: Locator '{value}' failed. Triggering Self-Healing!</b>", html=True)
                        except Exception:
                            builtin = None
                            
                        try:
                            # 1. Grab XML context directly from the active device
                            page_source = driver_self.page_source
                            
                            # 2. Heal the locator
                            new_locator = find_healed_locator(value, page_source)
                            
                            if new_locator:
                                if builtin:
                                    builtin.log(f"<b style='color:green'>SUCCESS: Healed locator discovered: '{new_locator}'. Retrying!</b>", html=True)
                                
                                # Healer logic returns pure XPATH string for this POC
                                from selenium.webdriver.common.by import By
                                new_by = By.XPATH
                                new_value = new_locator
                                if new_locator.startswith('xpath='):
                                    new_value = new_locator.replace('xpath=', '', 1)
                                
                                return original_find_element(driver_self, by=new_by, value=new_value)
                            else:
                                raise e # Bubble up original error
                                
                        except Exception:
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

    def _patch_appium_library(self):
        if self.appiumlib_patched:
            return
            
        try:
            builtin = BuiltIn()
            libraries = builtin.get_library_instances()
            for name, lib in libraries.items():
                if hasattr(lib, '_element_finder') and hasattr(lib._element_finder, 'find'):
                    if not getattr(lib._element_finder.find, '_is_healed', False):
                        original_find = lib._element_finder.find
                        
                        def create_healed_find(orig_find, capture_source_func):
                            def healed_find(locator, tag=None, required=True):
                                try:
                                    return orig_find(locator, tag, required)
                                except Exception as e:
                                    builtin.log(f"<b style='color:orange'>WARN: AppiumLibrary Locator '{locator}' failed. Triggering Self-Healing internally...</b>", html=True)
                                    try:
                                        page_source = capture_source_func()
                                        new_locator = find_healed_locator(locator, page_source)
                                        if new_locator:
                                            builtin.log(f"<b style='color:green'>SUCCESS: Healed locator discovered: '{new_locator}'. Retrying Appium action!</b>", html=True)
                                            return orig_find(new_locator, tag, required)
                                        else:
                                            raise e
                                    except Exception:
                                        raise e
                            healed_find._is_healed = True
                            return healed_find
                        
                        lib._element_finder.find = create_healed_find(original_find, lib.get_source)
                        self.appiumlib_patched = True
                        builtin.log(f"Self-Healing Engine activated on library: {name}.", "INFO")
        except Exception:
            pass
