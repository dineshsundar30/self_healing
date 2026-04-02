import traceback
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

class SelfHealingListener:
    """
    A Robot Framework Listener that globally monkey-patches AppiumLibrary to enable runtime self-healing.
    Usage:
    robot --listener SelfHealingListener.py my_tests.robot
    """
    
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.patch_applied = False

    def start_keyword(self, data, result):
        """
        Right before any keyword starts, we check if AppiumLibrary is loaded.
        If it is, and we haven't patched it yet, we patch it globally.
        This requires NO modifications to the user's .robot files.
        """
        if self.patch_applied:
            return

        try:
            builtin = BuiltIn()
            # Try to grab active AppiumLibrary instance
            appium_lib = builtin.get_library_instance('AppiumLibrary')
            
            # Patch the primary private method used for finding single elements.
            if hasattr(appium_lib, '_element_finder') and hasattr(appium_lib._element_finder, 'find'):
                original_find = appium_lib._element_finder.find
                
                def healed_find(locator, tag=None, required=True):
                    try:
                        # Attempt standard locator
                        return original_find(locator, tag, required)
                    except Exception as e:
                        builtin.log(f"<b style='color:orange'>WARN: Locator '{locator}' failed. Triggering Self-Healing internally...</b>", html=True)
                        
                        try:
                            # 1. Grab XML context directly from the active device
                            page_source = appium_lib.get_source()
                            
                            # 2. Heal the locator using local native python XML logic
                            new_locator = find_healed_locator(locator, page_source)
                            
                            if new_locator:
                                builtin.log(f"<b style='color:green'>SUCCESS: Healed locator discovered: '{new_locator}'. Retrying Appium action transparently!</b>", html=True)
                                # 3. Retry with new locator
                                return original_find(new_locator, tag, required)
                            else:
                                raise e # Bubble up original error
                                
                        except Exception as healing_err:
                            raise e

                # Apply Patch
                appium_lib._element_finder.find = healed_find
                self.patch_applied = True
                builtin.log("Self-Healing Engine activated globally via Listener. No test modifications required.", "INFO")
                
        except Exception:
            # AppiumLibrary is not imported yet in the test execution, we will try again next keyword
            pass
