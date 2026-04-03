import time
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

class SelfHealingListener:
    """
    A Robot Framework Listener that globally patches Appium WebDriver
    to enable runtime self-healing of locators.

    Key design decisions:
    - Only heals when the CALLER expects the element to be PRESENT.
      Negative assertions (status=FALSE / element-absence checks) are
      never healed so we don't turn an expected-absence into a false find.
    - Rate-limiting cache prevents healing storms inside Wait loops.
    - All internal errors are logged, never silently swallowed.
    """

    ROBOT_LISTENER_API_VERSION = 3

    # Keyword names (lower-cased) that look for element ABSENCE.
    # Healing must be suppressed when any of these are on the call stack.
    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",   # your custom keyword when status=FALSE
    })

    def __init__(self):
        self.webdriver_patched = False
        # {locator_value: expiry_timestamp}  — skip healing until expiry
        self.unhealable_cache: dict = {}
        # Track whether the current keyword tree expects absence
        self._negative_context: bool = False

    # ------------------------------------------------------------------
    # Listener hooks
    # ------------------------------------------------------------------

    def start_keyword(self, data, result):
        """Called before every keyword.  Detect negative-assertion context."""
        kw_name = (data.name or "").lower().strip()
        if kw_name in self._NEGATIVE_KEYWORDS:
            self._negative_context = True

        # Also detect via arguments: if 'status' arg equals 'FALSE'
        # handles your custom keyword: Check Elements Displayed  status=FALSE
        args = [str(a).upper() for a in (data.args or [])]
        if "STATUS=FALSE" in args or "FALSE" in args:
            self._negative_context = True

        self._patch_webdriver()

    def end_keyword(self, data, result):
        """Reset negative context after the keyword finishes."""
        kw_name = (data.name or "").lower().strip()
        if kw_name in self._NEGATIVE_KEYWORDS:
            self._negative_context = False

        args = [str(a).upper() for a in (data.args or [])]
        if "STATUS=FALSE" in args or "FALSE" in args:
            self._negative_context = False

    # ------------------------------------------------------------------
    # Core patch
    # ------------------------------------------------------------------

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
            from selenium.webdriver.common.by import By

            if getattr(WebDriver.find_element, '_is_healed', False):
                self.webdriver_patched = True
                return

            original_find_element = WebDriver.find_element
            unhealable_cache = self.unhealable_cache
            listener_ref = self  # reference to access _negative_context

            def healed_find_element(driver_self, by='id', value=None):
                # ── Happy path ──────────────────────────────────────────
                try:
                    return original_find_element(driver_self, by, value)
                except Exception as original_error:
                    pass  # fall through to healing logic

                # ── Guard: value must be a non-empty string ──────────────
                if not value or not isinstance(value, str):
                    raise original_error  # noqa: F821  (defined above)

                # ── Guard: never heal negative assertions ────────────────
                if listener_ref._negative_context:
                    raise original_error

                current_time = time.time()

                # ── Guard: rate-limit repeated failures ──────────────────
                if unhealable_cache.get(value, 0) > current_time:
                    raise original_error

                # ── Attempt healing ──────────────────────────────────────
                try:
                    builtin = BuiltIn()
                    builtin.log(
                        f"<b style='color:orange'>SELF-HEAL: Locator '{value}' failed. "
                        f"Capturing page source and attempting heal…</b>",
                        html=True
                    )

                    page_source = driver_self.page_source  # live XML dump
                    new_locator = find_healed_locator(value, page_source)

                    if new_locator:
                        builtin.log(
                            f"<b style='color:green'>SELF-HEAL SUCCESS: "
                            f"'{value}' → '{new_locator}'. Retrying…</b>",
                            html=True
                        )
                        # Healer always returns a pure XPath string
                        healed_value = new_locator
                        if healed_value.startswith('xpath='):
                            healed_value = healed_value[len('xpath='):]

                        try:
                            return original_find_element(
                                driver_self, by=By.XPATH, value=healed_value
                            )
                        except Exception:
                            builtin.log(
                                f"<b style='color:red'>SELF-HEAL: Healed locator "
                                f"'{healed_value}' also failed. Marking unhealable.</b>",
                                html=True
                            )
                            unhealable_cache[value] = current_time + 10.0
                            raise original_error
                    else:
                        builtin.log(
                            f"<b style='color:red'>SELF-HEAL: No candidate found "
                            f"for '{value}'. Marking unhealable for 10 s.</b>",
                            html=True
                        )
                        unhealable_cache[value] = current_time + 10.0
                        raise original_error

                except Exception as healing_pipeline_error:
                    # If the error IS our original_error it is already being re-raised
                    # correctly.  Any unexpected error in the pipeline: log + re-raise
                    # the ORIGINAL so the test sees the real problem.
                    if healing_pipeline_error is not original_error:
                        try:
                            BuiltIn().log(
                                f"SELF-HEAL pipeline error (suppressed): "
                                f"{healing_pipeline_error}",
                                "WARN"
                            )
                        except Exception:
                            pass
                    unhealable_cache[value] = current_time + 10.0
                    raise original_error

            healed_find_element._is_healed = True
            WebDriver.find_element = healed_find_element
            self.webdriver_patched = True

            try:
                BuiltIn().log(
                    "Self-Healing Engine activated (WebDriver patched).", "INFO"
                )
            except Exception:
                pass

        except ImportError as e:
            try:
                BuiltIn().log(
                    f"Self-Healing Engine could not patch WebDriver: {e}", "WARN"
                )
            except Exception:
                pass
        except Exception as e:
            try:
                BuiltIn().log(
                    f"Self-Healing Engine unexpected error during patch: {e}", "WARN"
                )
            except Exception:
                pass
