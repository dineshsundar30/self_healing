"""
SelfHealingListener.py
======================
Robot Framework Listener (API v3) — Self-healing engine for Appium XPath locators.

SUPPRESSION RULES (healing is skipped when ANY of these are true)
------------------------------------------------------------------
1. Run Keyword And Ignore Error          — caller does not care about failure
2. Run Keyword And Continue On Failure   — same intent
3. Run Keyword And Return Status         — result is inspected, not asserted
4. Element Should Not Be Visible / Exist — negative assertion
5. Check Elements Displayed status=FALSE — project-specific negative assertion
6. Test Setup / Test Teardown phase      — setup failures should not be healed
   (configurable via HEAL_IN_SETUP / HEAL_IN_TEARDOWN flags)
7. Locator value is empty or non-string
8. Locator is in the unhealable rate-limit cache

MULTIPLE-FAILURE TRACKING
--------------------------
Every locator failure is counted.  The count and a human-readable variable
name hint are logged every time, whether or not healing succeeds.

VARIABLE NAME RESOLUTION
-------------------------
After a failure the listener inspects the Robot Framework execution context
to find which ${VARIABLE} currently holds the failing XPath string.
This name is printed in every log line so you can immediately see which
page-object variable is broken.

LOGGING — every step is printed in colour-coded HTML visible in the RF report.
"""

import time
import traceback
from collections import defaultdict

from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator


class SelfHealingListener:

    ROBOT_LISTENER_API_VERSION = 3

    # ── Keyword names that SUPPRESS healing ─────────────────────────────────

    # These keywords explicitly ignore / swallow errors — healing would be wrong.
    _IGNORE_ERROR_KEYWORDS = frozenset({
        "run keyword and ignore error",
        "run keyword and continue on failure",
        "run keyword and return status",
        "run keyword and expect error",
        "run keywords",                          # bulk runner — conservative
        "wait until keyword succeeds",           # has its own retry logic
    })

    # These keywords assert an element must be ABSENT.
    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",              # project-specific
    })

    # ── Behaviour flags (change here if you want a different policy) ─────────
    HEAL_IN_SETUP     = False   # False = skip healing during Test Setup
    HEAL_IN_TEARDOWN  = False   # False = skip healing during Test Teardown

    # How long (seconds) to suppress re-healing after a locator proves unhealable
    UNHEALABLE_TTL    = 15.0

    # How many failures before we log a "persistent failure" warning
    PERSISTENT_FAIL_THRESHOLD = 3

    def __init__(self):
        self.webdriver_patched = False

        # {locator_str: expiry_float} — suppress repeated healing attempts
        self.unhealable_cache: dict = {}

        # {locator_str: int} — count how many times each locator has failed
        self.failure_counts: dict = defaultdict(int)

        # Suppression-context stack: each entry is a reason string.
        # Healing is suppressed whenever this stack is non-empty.
        self._suppress_stack: list = []

        # Phase tracking
        self._in_setup    = False
        self._in_teardown = False

    # ────────────────────────────────────────────────────────────────────────
    # Listener hooks
    # ────────────────────────────────────────────────────────────────────────

    def start_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False

    def start_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()   # 'setup'/'teardown'
        args_upper = [str(a).upper() for a in (data.args or [])]

        # ── Track setup / teardown phase ─────────────────────────────────────
        if kw_type == 'setup':
            self._in_setup = True
        elif kw_type == 'teardown':
            self._in_teardown = True

        # ── Push suppression reason if applicable ─────────────────────────────
        reason = None

        if kw_name in self._IGNORE_ERROR_KEYWORDS:
            reason = f"ignore-error wrapper: '{kw_name}'"

        elif kw_name in self._NEGATIVE_KEYWORDS:
            reason = f"negative assertion: '{kw_name}'"

        elif "STATUS=FALSE" in args_upper or "FALSE" in args_upper:
            reason = f"status=FALSE argument in '{kw_name}'"

        if reason:
            self._suppress_stack.append(reason)

        self._patch_webdriver()

    def end_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()
        args_upper = [str(a).upper() for a in (data.args or [])]

        # ── Pop suppression ───────────────────────────────────────────────────
        is_suppress_kw = (
            kw_name in self._IGNORE_ERROR_KEYWORDS
            or kw_name in self._NEGATIVE_KEYWORDS
            or "STATUS=FALSE" in args_upper
            or "FALSE" in args_upper
        )
        if is_suppress_kw and self._suppress_stack:
            self._suppress_stack.pop()

        # ── Clear phase flags ─────────────────────────────────────────────────
        if kw_type == 'setup':
            self._in_setup = False
        elif kw_type == 'teardown':
            self._in_teardown = False

    def end_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False
        self._suppress_stack.clear()

    # ────────────────────────────────────────────────────────────────────────
    # Suppression check
    # ────────────────────────────────────────────────────────────────────────

    def _should_suppress(self):
        """Return (suppress: bool, reason: str)."""
        if self._suppress_stack:
            return True, self._suppress_stack[-1]
        if self._in_setup and not self.HEAL_IN_SETUP:
            return True, "Test Setup phase (HEAL_IN_SETUP=False)"
        if self._in_teardown and not self.HEAL_IN_TEARDOWN:
            return True, "Test Teardown phase (HEAL_IN_TEARDOWN=False)"
        return False, ""

    # ────────────────────────────────────────────────────────────────────────
    # Variable name resolver
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_variable_name(xpath_value: str) -> str:
        """
        Search the current Robot variable scope for a variable whose value
        equals xpath_value.  Returns e.g. '${SAFETY_SCORE_TEXT}' or
        '<unknown variable>' if nothing matches.
        """
        try:
            builtin = BuiltIn()
            variables = builtin.get_variables()          # {${NAME}: value, ...}
            for var_name, var_val in variables.items():
                if isinstance(var_val, str) and var_val == xpath_value:
                    return var_name
        except Exception:
            pass
        return "<unknown variable>"

    # ────────────────────────────────────────────────────────────────────────
    # WebDriver patch
    # ────────────────────────────────────────────────────────────────────────

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
            from selenium.webdriver.common.by import By
        except ImportError as exc:
            self._safe_log(
                f"[SELF-HEAL] Appium import failed — engine disabled: {exc}", "WARN")
            self.webdriver_patched = True
            return

        if getattr(WebDriver.find_element, '_is_healed', False):
            self.webdriver_patched = True
            return

        original_find_element = WebDriver.find_element
        unhealable_cache      = self.unhealable_cache
        failure_counts        = self.failure_counts
        listener_ref          = self

        def healed_find_element(driver_self, by='id', value=None):
            # ── Step 1: Normal find — happy path ─────────────────────────────
            captured_error = None
            try:
                return original_find_element(driver_self, by, value)
            except Exception as exc:
                captured_error = exc   # copy immediately — Python deletes exc

            # ── Step 2: Normalise the value ───────────────────────────────────
            value_str = str(value).strip() if value is not None else ""

            # ── Step 3: Track failure count ───────────────────────────────────
            failure_counts[value_str] += 1
            fail_count = failure_counts[value_str]

            # ── Step 4: Resolve the Robot variable name ───────────────────────
            var_name = listener_ref._find_variable_name(value_str)

            # ── Step 5: Check suppression ─────────────────────────────────────
            suppressed, suppress_reason = listener_ref._should_suppress()

            if not value_str:
                listener_ref._safe_log(
                    "[SELF-HEAL] Skipping — locator value is empty.", "DEBUG")
                raise captured_error

            if suppressed:
                listener_ref._safe_log(
                    f"<span style='color:gray'>[SELF-HEAL] <b>SUPPRESSED</b> for "
                    f"<code>{var_name}</code> &rarr; <code>{value_str[:80]}</code><br>"
                    f"Reason: {suppress_reason} | Failures so far: {fail_count}</span>",
                    "DEBUG", html=True)
                raise captured_error

            now = time.time()
            if unhealable_cache.get(value_str, 0) > now:
                remaining = unhealable_cache[value_str] - now
                listener_ref._safe_log(
                    f"<span style='color:gray'>[SELF-HEAL] Rate-limited for "
                    f"<code>{var_name}</code> — {remaining:.1f}s remaining | "
                    f"Total failures: {fail_count}</span>",
                    "DEBUG", html=True)
                raise captured_error

            # ── Step 6: Log the failure with full context ─────────────────────
            is_persistent = fail_count >= listener_ref.PERSISTENT_FAIL_THRESHOLD
            fail_badge = (
                f"<span style='background:#cc0000;color:white;padding:1px 5px;"
                f"border-radius:3px'>PERSISTENT FAIL #{fail_count}</span>"
                if is_persistent else
                f"<span style='background:#e67e00;color:white;padding:1px 5px;"
                f"border-radius:3px'>FAIL #{fail_count}</span>"
            )

            listener_ref._safe_log(
                f"<b style='color:orange;font-size:13px'>&#9888; SELF-HEAL TRIGGERED</b> "
                f"{fail_badge}<br>"
                f"<table style='border-collapse:collapse;margin-top:4px'>"
                f"<tr><td style='padding:2px 8px 2px 0'><b>Variable&nbsp;</b></td>"
                f"    <td><code style='color:#9b59b6'>{var_name}</code></td></tr>"
                f"<tr><td><b>XPath&nbsp;&nbsp;&nbsp;</b></td>"
                f"    <td><code>{value_str}</code></td></tr>"
                f"<tr><td><b>By&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</b></td>"
                f"    <td>{by}</td></tr>"
                f"<tr><td><b>Error&nbsp;&nbsp;&nbsp;</b></td>"
                f"    <td>{type(captured_error).__name__}: "
                f"        {str(captured_error)[:200]}</td></tr>"
                f"</table>",
                "WARN", html=True)

            # ── Step 7: Capture live page source ──────────────────────────────
            page_source = None
            try:
                page_source = driver_self.page_source
                listener_ref._safe_log(
                    f"[SELF-HEAL] Page source captured — {len(page_source):,} chars.",
                    "INFO")
            except Exception as ps_exc:
                listener_ref._safe_log(
                    f"[SELF-HEAL] Cannot capture page source: {ps_exc}. Aborting.", "WARN")
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── Step 8: Run healer logic ──────────────────────────────────────
            new_locator = None
            try:
                new_locator = find_healed_locator(value_str, page_source)
            except Exception as healer_exc:
                listener_ref._safe_log(
                    f"[SELF-HEAL] HealerLogic exception for {var_name}:<br>"
                    f"<pre>{traceback.format_exc()}</pre>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── Step 9: No match found ────────────────────────────────────────
            if not new_locator:
                listener_ref._safe_log(
                    f"<b style='color:red'>&#10007; NO MATCHING XPATH FOUND</b><br>"
                    f"<table style='border-collapse:collapse'>"
                    f"<tr><td style='padding:2px 8px 2px 0'><b>Variable</b></td>"
                    f"    <td><code style='color:#9b59b6'>{var_name}</code></td></tr>"
                    f"<tr><td><b>XPath</b></td>"
                    f"    <td><code>{value_str}</code></td></tr>"
                    f"<tr><td><b>Failures</b></td><td>{fail_count}</td></tr>"
                    f"</table>"
                    f"<br>Possible causes:<br>"
                    f"&bull; All tokens in the locator are generic or too short (&lt;4 chars)<br>"
                    f"&bull; Element genuinely does not exist on this screen<br>"
                    f"&bull; App is mid-transition or screen has not loaded yet<br>"
                    f"&bull; Resource-ID was renamed and no similar ID exists in current XML",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── Step 10: Try the healed locator ──────────────────────────────
            healed_xpath = new_locator.lstrip("xpath=") if new_locator.startswith("xpath=") else new_locator

            listener_ref._safe_log(
                f"<b style='color:#0077cc'>&#128270; HEAL CANDIDATE FOUND</b><br>"
                f"<table style='border-collapse:collapse'>"
                f"<tr><td style='padding:2px 8px 2px 0'><b>Variable&nbsp;</b></td>"
                f"    <td><code style='color:#9b59b6'>{var_name}</code></td></tr>"
                f"<tr><td><b>Original</b></td>"
                f"    <td><code style='color:red'>{value_str}</code></td></tr>"
                f"<tr><td><b>Healed&nbsp;</b></td>"
                f"    <td><code style='color:green'>{healed_xpath}</code></td></tr>"
                f"</table>",
                "INFO", html=True)

            try:
                found = original_find_element(driver_self, by=By.XPATH, value=healed_xpath)
                listener_ref._safe_log(
                    f"<b style='color:green;font-size:13px'>&#10003; SELF-HEAL SUCCESS</b><br>"
                    f"<table style='border-collapse:collapse'>"
                    f"<tr><td style='padding:2px 8px 2px 0'><b>Variable&nbsp;</b></td>"
                    f"    <td><code style='color:#9b59b6'>{var_name}</code></td></tr>"
                    f"<tr><td><b>Healed&nbsp;</b></td>"
                    f"    <td><code style='color:green'>{healed_xpath}</code></td></tr>"
                    f"<tr><td><b>Total failures before heal</b></td>"
                    f"    <td>{fail_count}</td></tr>"
                    f"</table>",
                    "INFO", html=True)
                # Reset failure count on success
                failure_counts[value_str] = 0
                return found

            except Exception as retry_exc:
                listener_ref._safe_log(
                    f"<b style='color:red'>&#10007; HEALED LOCATOR ALSO FAILED</b><br>"
                    f"<table style='border-collapse:collapse'>"
                    f"<tr><td style='padding:2px 8px 2px 0'><b>Variable&nbsp;</b></td>"
                    f"    <td><code style='color:#9b59b6'>{var_name}</code></td></tr>"
                    f"<tr><td><b>Tried&nbsp;</b></td>"
                    f"    <td><code>{healed_xpath}</code></td></tr>"
                    f"<tr><td><b>Error&nbsp;</b></td>"
                    f"    <td>{type(retry_exc).__name__}: {str(retry_exc)[:200]}</td></tr>"
                    f"</table>"
                    f"<br>Marking as unhealable for {listener_ref.UNHEALABLE_TTL:.0f}s.",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

        # ── Install the patch ────────────────────────────────────────────────
        healed_find_element._is_healed = True
        WebDriver.find_element = healed_find_element
        self.webdriver_patched = True
        self._safe_log(
            "<b style='color:green'>&#9889; SELF-HEAL ENGINE ACTIVE — "
            "WebDriver.find_element patched globally.</b>",
            "INFO", html=True)

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_log(message: str, level: str = "INFO", html: bool = False):
        """Log via Robot BuiltIn. Never raises under any circumstance."""
        try:
            BuiltIn().log(message, level, html=html)
        except Exception:
            pass
