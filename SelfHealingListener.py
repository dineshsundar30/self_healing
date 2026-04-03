import traceback
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

# Keywords we want to wrap with self-healing logic.
# By wrapping explicit action keywords rather than low-level find_element,
# we avoid destroying performance on negative assertions (e.g. Wait Until Element Is Not Visible)
TARGET_METHODS = [
    'click_element',
    'input_text',
    'input_password',
    'clear_text',
    'get_element_by_xpath',
    'get_webelement',
    'tap'
]

class SelfHealingListener:
    """
    A Robot Framework Listener that patches specific high-level Appium action 
    keywords to enable runtime self-healing of locators without disturbing setup loops.
    """
    
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.patched_libraries = set()

    def start_keyword(self, data, result):
        # Patch libraries dynamically exactly once per library
        try:
            builtin = BuiltIn()
            libraries = builtin.get_library_instances()
            
            for name, lib in libraries.items():
                if name not in self.patched_libraries:
                    self._patch_library_methods(name, lib, builtin)
                    self.patched_libraries.add(name)
        except Exception:
            pass

    def _patch_library_methods(self, name, lib, builtin):
        # We only patch libraries that seem to interact with Appium/UI
        if not hasattr(lib, '_current_browser') and not hasattr(lib, 'driver') and 'Appium' not in name:
            return

        for method_name in TARGET_METHODS:
            if hasattr(lib, method_name):
                original_method = getattr(lib, method_name)
                
                # Check if it's a callable and not already patched
                if callable(original_method) and not getattr(original_method, '_is_healed', False):
                    
                    def create_healed_method(orig_method, lib_instance, m_name):
                        def healed_wrapper(locator, *args, **kwargs):
                            try:
                                return orig_method(locator, *args, **kwargs)
                            except Exception as e:
                                # Intercept and heal ONLY if it's a targeted action keyword failing
                                try:
                                    builtin.log(f"<b style='color:orange'>WARN: Keyword '{m_name}' failed on locator '{locator}'. Triggering Self-Healing!</b>", html=True)
                                    
                                    # Fetch page source
                                    page_source = None
                                    if hasattr(lib_instance, 'get_source'):
                                        page_source = lib_instance.get_source()
                                    elif hasattr(lib_instance, 'driver'):
                                        page_source = lib_instance.driver.page_source
                                    elif hasattr(lib_instance, '_current_browser'):
                                        page_source = lib_instance._current_browser().page_source
                                        
                                    if page_source:
                                        new_locator = find_healed_locator(locator, page_source)
                                        if new_locator:
                                            builtin.log(f"<b style='color:green'>SUCCESS: Healed locator discovered: '{new_locator}'. Retrying!</b>", html=True)
                                            
                                            # Optional strict format handling depending on the method
                                            if m_name == 'get_element_by_xpath' and new_locator.startswith('xpath='):
                                                new_locator = new_locator.replace('xpath=', '', 1)
                                                
                                            return orig_method(new_locator, *args, **kwargs)
                                    raise e
                                except Exception:
                                    raise e
                        
                        healed_wrapper._is_healed = True
                        return healed_wrapper
                    
                    setattr(lib, method_name, create_healed_method(original_method, lib, method_name))
                    try:
                        builtin.log(f"Self-Healing Engine activated on keyword wrapper: {name}.{method_name}", "INFO")
                    except:
                        pass
