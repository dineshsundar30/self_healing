import time
from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator


class SelfHealingListener:
    """
    A Robot Framework Listener that globally patches Appium WebDriver
    to enable runtime self-healing of broken XPath / locators.

    Key design decisions:
    - Only heals when the caller expects the element to be PRESENT.
      Negative assertions (status=FALSE / element-absence checks) are
      never healed so we don't turn an expected-absence into a false find.
    - Rate-limiting cache prevents healing storms inside Wait loops.
    - Exception variable is saved to a plain local immediately so Python 3's
      automatic deletion of 'except ... as e' variables never causes
      UnboundLocalError.
    """

    ROBOT_LISTENER_API_VERSION = 3

    # Keyword names (lower-cased) that assert element ABSENCE.
    # Healing must be suppressed whenever these are on the call stack.
    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",   # custom keyword when status=FALSE
    })

    def __init__(self):
        self.webdriver_patched = False
        # {locator_value: expiry_timestamp} — skip healing until expiry
        self.unhealable_cache: dict = {}
        # True while we are inside a keyword that asserts element absence
        self._negative_context: bool = False

    # ------------------------------------------------------------------
    # Listener hooks
    # ------------------------------------------------------------------

    def start_keyword(self, data, result):
        """Detect negative-assertion context, then ensure driver is patched."""
        kw_name = (data.name or "").lower().strip()
        if kw_name in self._NEGATIVE_KEYWORDS:
            self._negative_context = True

        # Detect via named/positional arg: status=FALSE  or bare FALSE
        args = [str(a).upper() for a in (data.args or [])]
        if "STATUS=FALSE" in args or "FALSE" in args:
            self._negative_context = True

        self._patch_webdriver()

    def end_keyword(self, data, result):
        """Reset negative context once the keyword finishes."""
        kw_name = (data.name or "").lower().strip()
        if kw_name in self._NEGATIVE_KEYWORDS:
            self._negative_context = False

        args = [str(a).upper() for a in (data.args or [])]
        if "STATUS=FALSE" in args or "FALSE" in args:
            self._negative_context = False

    # ------------------------------------------------------------------
    # Core WebDriver patch
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
            unhealable_cache      = self.unhealable_cache
            listener_ref          = self

            def healed_find_element(driver_self, by='id', value=None):
                # ── 1. Happy path ────────────────────────────────────────
                # IMPORTANT: we immediately copy the exception to a plain
                # local variable.  Python 3 automatically deletes the
                # 'except ... as name' variable at the end of the except
                # block, so any later 'raise original_error' would be an
                # UnboundLocalError.  Copying to captured_error avoids this.
                captured_error = None
                try:
                    return original_find_element(driver_self, by, value)
                except Exception as exc:
                    captured_error = exc   # save before Python deletes exc

                # ── 2. Basic guards ──────────────────────────────────────
                if not value or not isinstance(value, str):
                    raise captured_error

                if listener_ref._negative_context:
                    raise captured_error

                current_time = time.time()

                if unhealable_cache.get(value, 0) > current_time:
                    raise captured_error

                # ── 3. Healing pipeline ──────────────────────────────────
                healing_succeeded = False
                try:
                    builtin = BuiltIn()
                    builtin.log(
                        f"<b style='color:orange'>SELF-HEAL: Locator '{value}' failed. "
                        f"Capturing page source and attempting heal...</b>",
                        html=True
                    )

                    page_source = driver_self.page_source       # live XML dump
                    new_locator = find_healed_locator(value, page_source)

                    if new_locator:
                        healed_value = new_locator
                        if healed_value.startswith('xpath='):
                            healed_value = healed_value[len('xpath='):]

                        builtin.log(
                            f"<b style='color:green'>SELF-HEAL SUCCESS: "
                            f"'{value}' -> '{healed_value}'. Retrying...</b>",
                            html=True
                        )

                        try:
                            found = original_find_element(
                                driver_self, by=By.XPATH, value=healed_value
                            )
                            healing_succeeded = True
                            return found
                        except Exception as retry_exc:
                            # save retry error for logging, then fall through
                            builtin.log(
                                f"<b style='color:red'>SELF-HEAL: Healed locator "
                                f"'{healed_value}' also failed ({retry_exc}). "
                                f"Marking unhealable.</b>",
                                html=True
                            )
                            unhealable_cache[value] = current_time + 10.0

                    else:
                        builtin.log(
                            f"<b style='color:red'>SELF-HEAL: No candidate found "
                            f"for '{value}'. Marking unhealable for 10 s.</b>",
                            html=True
                        )
                        unhealable_cache[value] = current_time + 10.0

                except Exception as pipeline_exc:
                    if not healing_succeeded:
                        if pipeline_exc is not captured_error:
                            try:
                                BuiltIn().log(
                                    f"SELF-HEAL pipeline error (suppressed): "
                                    f"{pipeline_exc}",
                                    "WARN"
                                )
                            except Exception:
                                pass
                        unhealable_cache[value] = current_time + 10.0

                # ── 4. All paths exhausted — bubble up the original error ─
                raise captured_error

            healed_find_element._is_healed = True
            WebDriver.find_element = healed_find_element
            self.webdriver_patched = True

            try:
                BuiltIn().log(
                    "Self-Healing Engine activated (WebDriver patched).", "INFO"
                )
            except Exception:
                pass

        except ImportError as exc:
            try:
                BuiltIn().log(
                    f"Self-Healing Engine could not patch WebDriver "
                    f"(Appium not found): {exc}", "WARN"
                )
            except Exception:
                pass
        except Exception as exc:
            try:
                BuiltIn().log(
                    f"Self-Healing Engine unexpected patch error: {exc}", "WARN"
                )
            except Exception:
                pass
