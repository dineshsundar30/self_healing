"""
SelfHealingListener.py
Robot Framework Listener (API v3) — Automatically heals failing Appium locators.
"""
import time
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

class SelfHealingListener:
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self):
        self.patched = False

    def start_suite(self, data, result):
        """Patch the WebDriver as soon as the suite starts."""
        self._patch_appium()

    def _patch_appium(self):
        if self.patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
        except ImportError:
            self._log("Appium library not found. Self-healing disabled.", "WARN")
            return

        original_find_element = WebDriver.find_element

        def healed_find_element(driver_self, by='id', value=None):
            try:
                # Attempt standard execution
                return original_find_element(driver_self, by, value)
            except Exception as original_error:
                # If it fails, start the healing process
                start_time = time.time()
                self._log(f"<b>[SELF-HEAL]</b> Attempting to heal: <code>{value}</code>", "WARN", html=True)

                try:
                    page_source = driver_self.page_source
                    new_locator = find_healed_locator(str(value), page_source)
                    
                    if new_locator:
                        self._log(f"<b>[SELF-HEAL]</b> Success! New Locator: <code>{new_locator}</code> (Time: {time.time()-start_time:.2f}s)", "INFO", html=True)
                        # Retry with the healed XPath
                        clean_xpath = new_locator.replace("xpath=", "")
                        return original_find_element(driver_self, "xpath", clean_xpath)
                except Exception as healer_error:
                    self._log(f"[SELF-HEAL] Healer crashed: {healer_error}", "DEBUG")

                # If healing fails, raise the original error so the test fails normally
                raise original_error

        WebDriver.find_element = healed_find_element
        self.patched = True
        self._log("<b style='color:green'>Self-Healing Engine Active</b>", "INFO", html=True)

    def _log(self, message, level="INFO", html=False):
        try:
            BuiltIn().log(message, level, html=html)
        except:
            print(f"[{level}] {message}")
