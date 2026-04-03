"""
SelfHealingListener.py
======================
Robot Framework Listener (API v3) that monkey-patches Appium's WebDriver
so every failed find_element() attempt is automatically healed at runtime.

What this file guarantees
--------------------------
1. captured_error is ALWAYS a plain local — never the 'except ... as e'
   variable that Python 3 deletes at end-of-block (UnboundLocalError fix).

2. The patch intercepts ALL exception types from find_element, not just
   NoSuchElementException — including Appium's ExceptionHandlerError,
   StaleElementReferenceException, WebDriverException, etc.

3. Healing is SUPPRESSED for negative assertions (element must be absent).
   Detection is by keyword name AND by 'status=FALSE' / 'FALSE' args.

4. A per-locator rate-limit cache (10 s) prevents healing storms inside
   'Wait Until Keyword Succeeds' polling loops.

5. Every step is logged with colour-coded HTML so you can trace exactly
   what happened in the Robot report.

6. Any exception raised INSIDE the healing pipeline is logged as WARN
   and the ORIGINAL Appium error is always what bubbles up to the test —
   the healer can never make things worse.
"""

import time
import traceback

from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator


class SelfHealingListener:

    ROBOT_LISTENER_API_VERSION = 3

    # ── Keyword names that assert an element must be ABSENT ─────────────────
    # Healing is completely suppressed inside these keywords so a correct
    # "element not found" result is never accidentally turned into a find.
    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",   # project-specific; triggered by status=FALSE
    })

    def __init__(self):
        self.webdriver_patched   = False
        self.unhealable_cache: dict = {}   # {value_str: expiry_float}
        self._negative_context: bool = False

    # ────────────────────────────────────────────────────────────────────────
    # Listener hooks
    # ────────────────────────────────────────────────────────────────────────

    def start_keyword(self, data, result):
        self._update_negative_context(data, entering=True)
        self._patch_webdriver()

    def end_keyword(self, data, result):
        self._update_negative_context(data, entering=False)

    def _update_negative_context(self, data, entering: bool):
        """Set / clear _negative_context based on keyword name and args."""
        kw_name = (data.name or "").lower().strip()
        is_negative_kw = kw_name in self._NEGATIVE_KEYWORDS

        # Detect status=FALSE or bare FALSE in any argument position
        args_upper = [str(a).upper() for a in (data.args or [])]
        has_false_arg = "STATUS=FALSE" in args_upper or "FALSE" in args_upper

        if entering:
            if is_negative_kw or has_false_arg:
                self._negative_context = True
        else:
            # Only clear if THIS keyword was the one that set the flag
            if is_negative_kw or has_false_arg:
                self._negative_context = False

    # ────────────────────────────────────────────────────────────────────────
    # WebDriver patch — applied once, survives for the whole suite
    # ────────────────────────────────────────────────────────────────────────

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
            from selenium.webdriver.common.by import By
        except ImportError as exc:
            self._safe_log(f"[SELF-HEAL] Appium import failed — engine disabled: {exc}", "WARN")
            self.webdriver_patched = True   # don't retry
            return

        if getattr(WebDriver.find_element, '_is_healed', False):
            self.webdriver_patched = True
            return

        original_find_element = WebDriver.find_element
        unhealable_cache      = self.unhealable_cache
        listener_ref          = self

        # ── The replacement find_element ─────────────────────────────────────
        def healed_find_element(driver_self, by='id', value=None):
            """
            Drop-in replacement for WebDriver.find_element.
            Tries the original call first; on ANY failure attempts self-healing.
            """

            # ── Step 1: Normal find — happy path ─────────────────────────────
            captured_error = None          # plain local, never auto-deleted
            try:
                return original_find_element(driver_self, by, value)
            except Exception as exc:
                captured_error = exc       # copy NOW — Python deletes exc here

            # ── Step 2: Should we even attempt healing? ───────────────────────
            value_str = str(value) if value is not None else ""

            if not value_str:
                listener_ref._safe_log(
                    "[SELF-HEAL] Skipping heal — locator value is empty.", "DEBUG")
                raise captured_error

            if listener_ref._negative_context:
                listener_ref._safe_log(
                    f"[SELF-HEAL] Skipping heal for '{value_str}' — "
                    f"inside a negative-assertion context (element expected absent).", "DEBUG")
                raise captured_error

            now = time.time()
            if unhealable_cache.get(value_str, 0) > now:
                listener_ref._safe_log(
                    f"[SELF-HEAL] Rate-limited: '{value_str}' is in unhealable cache. "
                    f"Suppressing heal for {unhealable_cache[value_str] - now:.1f}s more.", "DEBUG")
                raise captured_error

            # ── Step 3: Log the failure with full detail ──────────────────────
            listener_ref._safe_log(
                f"<b style='color:orange;font-size:13px'>"
                f"&#9888; SELF-HEAL TRIGGERED</b><br>"
                f"<b>By:</b> {by}<br>"
                f"<b>Value:</b> <code>{value_str}</code><br>"
                f"<b>Original error type:</b> {type(captured_error).__name__}<br>"
                f"<b>Original error msg:</b> {str(captured_error)[:300]}",
                "WARN", html=True
            )

            # ── Step 4: Capture live page source ─────────────────────────────
            page_source = None
            try:
                page_source = driver_self.page_source
                listener_ref._safe_log(
                    f"[SELF-HEAL] Page source captured — "
                    f"{len(page_source)} chars.", "INFO")
            except Exception as ps_exc:
                listener_ref._safe_log(
                    f"[SELF-HEAL] Could not capture page source: {ps_exc}. "
                    f"Healing aborted.", "WARN")
                unhealable_cache[value_str] = now + 10.0
                raise captured_error

            # ── Step 5: Run healer logic ──────────────────────────────────────
            new_locator = None
            try:
                new_locator = find_healed_locator(value_str, page_source)
            except Exception as healer_exc:
                listener_ref._safe_log(
                    f"[SELF-HEAL] HealerLogic raised an exception: {healer_exc}<br>"
                    f"<pre>{traceback.format_exc()}</pre>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + 10.0
                raise captured_error

            if not new_locator:
                listener_ref._safe_log(
                    f"<b style='color:red'>[SELF-HEAL] No healing candidate found "
                    f"for <code>{value_str}</code>.</b><br>"
                    f"Possible reasons:<br>"
                    f"&bull; All keywords in the locator are too generic (&lt;5 chars or in ignore list)<br>"
                    f"&bull; Element genuinely does not exist on this screen<br>"
                    f"&bull; App is mid-transition / loading",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + 10.0
                raise captured_error

            # ── Step 6: Try the healed locator ───────────────────────────────
            healed_xpath = new_locator
            if healed_xpath.startswith('xpath='):
                healed_xpath = healed_xpath[len('xpath='):]

            listener_ref._safe_log(
                f"<b style='color:#0077cc'>[SELF-HEAL] Candidate found!</b><br>"
                f"<b>Original:</b> <code>{value_str}</code><br>"
                f"<b>Healed&nbsp;&nbsp;:</b> <code>{healed_xpath}</code><br>"
                f"Retrying with healed XPath...",
                "INFO", html=True)

            try:
                found = original_find_element(driver_self, by=By.XPATH, value=healed_xpath)
                listener_ref._safe_log(
                    f"<b style='color:green;font-size:13px'>"
                    f"&#10003; SELF-HEAL SUCCESS</b><br>"
                    f"Healed locator works: <code>{healed_xpath}</code>",
                    "INFO", html=True)
                return found                # ← success, element returned

            except Exception as retry_exc:
                listener_ref._safe_log(
                    f"<b style='color:red'>[SELF-HEAL] Healed locator also failed.</b><br>"
                    f"<b>Healed XPath:</b> <code>{healed_xpath}</code><br>"
                    f"<b>Error:</b> {type(retry_exc).__name__}: {str(retry_exc)[:200]}<br>"
                    f"Marking as unhealable for 10 s.",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + 10.0
                raise captured_error       # always raise the ORIGINAL error

        # ── Install the patch ────────────────────────────────────────────────
        healed_find_element._is_healed = True
        WebDriver.find_element = healed_find_element
        self.webdriver_patched = True
        self._safe_log(
            "<b style='color:green'>[SELF-HEAL] Engine ACTIVE — "
            "WebDriver.find_element patched globally.</b>",
            "INFO", html=True)

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_log(message: str, level: str = "INFO", html: bool = False):
        """Log via BuiltIn; never raises even if called outside a test."""
        try:
            if html:
                BuiltIn().log(message, level, html=True)
            else:
                BuiltIn().log(message, level)
        except Exception:
            pass   # called before suite starts or after it ends — ignore
