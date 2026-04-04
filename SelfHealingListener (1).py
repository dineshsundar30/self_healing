"""
SelfHealingListener.py
======================
Robot Framework Listener (API v3) — Self-healing engine for Appium XPath locators.

╔══════════════════════════════════════════════════════════════════════════════╗
║  TO ENABLE HEALING IN SETUP / TEARDOWN — change these two lines:           ║
║      HEAL_IN_SETUP    = False   →   HEAL_IN_SETUP    = True                ║
║      HEAL_IN_TEARDOWN = False   →   HEAL_IN_TEARDOWN = True                ║
╚══════════════════════════════════════════════════════════════════════════════╝

SUPPRESSION RULES  (healing is completely skipped when ANY rule matches)
------------------------------------------------------------------------
1. Run Keyword And Ignore Error / Continue On Failure / Return Status
   → caller intentionally ignores failure, healing would be wrong
2. Negative-assertion keywords  (Element Should Not Exist, etc.)
3. Check Elements Displayed  status=FALSE  (project-specific)
4. Test Setup phase   (controlled by HEAL_IN_SETUP  flag above)
5. Test Teardown phase (controlled by HEAL_IN_TEARDOWN flag above)
6. Locator value is empty
7. Locator is in the per-locator rate-limit cache

LOGGING PHILOSOPHY
------------------
• Every single event is logged at INFO or WARN — nothing at DEBUG.
  (DEBUG is hidden in Robot reports unless --loglevel DEBUG is passed.)
• Every log line includes the Robot variable name (e.g. ${SAFETY_SCORE_TEXT})
  AND the raw XPath so you can pinpoint the broken locator instantly.
• Suppressed events are also logged at INFO so you can confirm the engine
  saw the failure and deliberately chose not to heal it.
• A one-line HEAL SUMMARY banner is written to console (via print) in
  addition to the HTML report so it is impossible to miss.
"""

import time
import traceback
from collections import defaultdict

from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator


# ── ANSI colours for console output (visible in terminal / CI logs) ──────────
_C_RESET  = "\033[0m"
_C_ORANGE = "\033[33m"
_C_GREEN  = "\033[32m"
_C_RED    = "\033[31m"
_C_CYAN   = "\033[36m"
_C_GRAY   = "\033[90m"
_C_BOLD   = "\033[1m"


class SelfHealingListener:

    ROBOT_LISTENER_API_VERSION = 3

    # ╔══════════════════════════════════════════════════════════════╗
    # ║           ★  CONFIGURATION — EDIT HERE  ★                  ║
    # ╚══════════════════════════════════════════════════════════════╝

    # Set to True to enable healing during Test Setup keywords
    HEAL_IN_SETUP     = False

    # Set to True to enable healing during Test Teardown keywords
    HEAL_IN_TEARDOWN  = False

    # Seconds to suppress re-healing after a locator proves unhealable
    UNHEALABLE_TTL    = 15.0

    # Failure count threshold before log entry turns "PERSISTENT FAIL"
    PERSISTENT_FAIL_THRESHOLD = 3

    # ── Keyword names that suppress healing ──────────────────────────────────

    _IGNORE_ERROR_KEYWORDS = frozenset({
        "run keyword and ignore error",
        "run keyword and continue on failure",
        "run keyword and return status",
        "run keyword and expect error",
        "run keywords",
        "wait until keyword succeeds",
    })

    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",
    })

    # ────────────────────────────────────────────────────────────────────────

    def __init__(self):
        self.webdriver_patched  = False
        self.unhealable_cache: dict        = {}               # {xpath: expiry}
        self.failure_counts: dict          = defaultdict(int) # {xpath: count}
        self._suppress_stack: list         = []               # suppression reasons
        self._in_setup                     = False
        self._in_teardown                  = False

    # ────────────────────────────────────────────────────────────────────────
    # Listener hooks
    # ────────────────────────────────────────────────────────────────────────

    def start_suite(self, data, result):
        self._console(f"{_C_CYAN}{_C_BOLD}[SELF-HEAL] Engine starting for suite: "
                      f"{data.name}{_C_RESET}")
        self._console(f"{_C_CYAN}[SELF-HEAL] HEAL_IN_SETUP={self.HEAL_IN_SETUP}  "
                      f"HEAL_IN_TEARDOWN={self.HEAL_IN_TEARDOWN}{_C_RESET}")

    def start_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False
        self._suppress_stack.clear()

    def end_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False
        self._suppress_stack.clear()

    def start_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()
        args_upper = [str(a).upper() for a in (data.args or [])]

        # Track setup / teardown phase via keyword type
        if kw_type == 'setup':
            self._in_setup    = True
            self._in_teardown = False
        elif kw_type == 'teardown':
            self._in_teardown = True
            self._in_setup    = False

        # Push suppression reason if this keyword suppresses healing
        reason = self._suppression_reason_for(kw_name, args_upper)
        if reason:
            self._suppress_stack.append(reason)

        self._patch_webdriver()

    def end_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()
        args_upper = [str(a).upper() for a in (data.args or [])]

        if self._suppression_reason_for(kw_name, args_upper) and self._suppress_stack:
            self._suppress_stack.pop()

        if kw_type == 'setup':
            self._in_setup    = False
        elif kw_type == 'teardown':
            self._in_teardown = False

    # ────────────────────────────────────────────────────────────────────────
    # Suppression helpers
    # ────────────────────────────────────────────────────────────────────────

    def _suppression_reason_for(self, kw_name: str, args_upper: list):
        """Return a reason string if this keyword should suppress healing, else None."""
        if kw_name in self._IGNORE_ERROR_KEYWORDS:
            return f"ignore-error wrapper '{kw_name}'"
        if kw_name in self._NEGATIVE_KEYWORDS:
            return f"negative-assertion keyword '{kw_name}'"
        if "STATUS=FALSE" in args_upper or "FALSE" in args_upper:
            return f"status=FALSE argument in '{kw_name}'"
        return None

    def _get_suppression(self):
        """Return (is_suppressed: bool, reason: str)."""
        if self._suppress_stack:
            return True, self._suppress_stack[-1]
        if self._in_setup and not self.HEAL_IN_SETUP:
            return True, "Test Setup phase — set HEAL_IN_SETUP=True to enable"
        if self._in_teardown and not self.HEAL_IN_TEARDOWN:
            return True, "Test Teardown phase — set HEAL_IN_TEARDOWN=True to enable"
        return False, ""

    # ────────────────────────────────────────────────────────────────────────
    # Variable name resolver
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_variable_name(xpath_value: str) -> str:
        """
        Scan the current Robot variable scope for a variable whose value
        equals xpath_value.  Returns e.g. '${SAFETY_SCORE_TEXT}' or
        '<unknown variable>' when nothing matches.
        """
        try:
            variables = BuiltIn().get_variables()
            for var_name, var_val in variables.items():
                if isinstance(var_val, str) and var_val == xpath_value:
                    return var_name
        except Exception:
            pass
        return "<unknown variable>"

    # ────────────────────────────────────────────────────────────────────────
    # WebDriver patch — installed once, active for the whole suite
    # ────────────────────────────────────────────────────────────────────────

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
            from selenium.webdriver.common.by import By
        except ImportError as exc:
            self._log(f"[SELF-HEAL] Appium import failed — engine disabled: {exc}", "WARN")
            self.webdriver_patched = True
            return

        if getattr(WebDriver.find_element, '_is_healed', False):
            self.webdriver_patched = True
            return

        original_find_element = WebDriver.find_element
        unhealable_cache       = self.unhealable_cache
        failure_counts         = self.failure_counts
        listener_ref           = self

        def healed_find_element(driver_self, by='id', value=None):

            # ── STEP 1: Normal find ─────────────────────────────────────────
            captured_error = None
            try:
                return original_find_element(driver_self, by, value)
            except Exception as exc:
                captured_error = exc        # save immediately — Python deletes exc

            # ── STEP 2: Normalise value ─────────────────────────────────────
            value_str = str(value).strip() if value is not None else ""

            # ── STEP 3: Count the failure ───────────────────────────────────
            failure_counts[value_str] += 1
            fail_count = failure_counts[value_str]

            # ── STEP 4: Resolve Robot variable name ─────────────────────────
            var_name = listener_ref._find_variable_name(value_str)

            # ── STEP 5: Empty locator guard ─────────────────────────────────
            if not value_str:
                listener_ref._log("[SELF-HEAL] Skipping — locator value is empty.", "WARN")
                raise captured_error

            # ── STEP 6: Check suppression ───────────────────────────────────
            suppressed, suppress_reason = listener_ref._get_suppression()
            if suppressed:
                listener_ref._log(
                    f"<div style='border-left:3px solid #aaa;padding:4px 8px;"
                    f"background:#f9f9f9;margin:2px 0'>"
                    f"<b>[SELF-HEAL] SUPPRESSED</b> &mdash; healing skipped<br>"
                    f"<b>Variable&nbsp;:</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>XPath&nbsp;&nbsp;&nbsp;&nbsp;:</b> <code>{value_str[:120]}</code><br>"
                    f"<b>Reason&nbsp;&nbsp;&nbsp;:</b> {suppress_reason}<br>"
                    f"<b>Fail count:</b> {fail_count}"
                    f"</div>",
                    "INFO", html=True)
                raise captured_error

            # ── STEP 7: Rate-limit check ────────────────────────────────────
            now = time.time()
            if unhealable_cache.get(value_str, 0) > now:
                remaining = unhealable_cache[value_str] - now
                listener_ref._log(
                    f"<div style='border-left:3px solid #aaa;padding:4px 8px;"
                    f"background:#f9f9f9;margin:2px 0'>"
                    f"<b>[SELF-HEAL] RATE-LIMITED</b> — already marked unhealable<br>"
                    f"<b>Variable&nbsp;:</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>XPath&nbsp;&nbsp;&nbsp;&nbsp;:</b> <code>{value_str[:120]}</code><br>"
                    f"<b>Retry in&nbsp;:</b> {remaining:.1f}s | "
                    f"<b>Fail count:</b> {fail_count}"
                    f"</div>",
                    "INFO", html=True)
                raise captured_error

            # ── STEP 8: Log the trigger (always INFO + console) ─────────────
            is_persistent = fail_count >= listener_ref.PERSISTENT_FAIL_THRESHOLD
            badge_colour  = "#cc0000" if is_persistent else "#e67e00"
            badge_text    = f"PERSISTENT FAIL #{fail_count}" if is_persistent else f"FAIL #{fail_count}"

            listener_ref._console(
                f"{_C_ORANGE}{_C_BOLD}[SELF-HEAL] ⚠ TRIGGERED  "
                f"var={var_name}  failures={fail_count}{_C_RESET}")

            listener_ref._log(
                f"<div style='border:2px solid {badge_colour};border-radius:4px;"
                f"padding:6px 10px;margin:4px 0;background:#fff8f0'>"
                f"<b style='font-size:13px;color:{badge_colour}'>⚠ SELF-HEAL TRIGGERED</b>"
                f"<span style='background:{badge_colour};color:white;padding:1px 6px;"
                f"border-radius:3px;margin-left:8px;font-size:11px'>{badge_text}</span><br><br>"
                f"<b>Variable&nbsp;</b> : <code style='color:#9b59b6'>{var_name}</code><br>"
                f"<b>XPath&nbsp;&nbsp;&nbsp;&nbsp;</b> : <code>{value_str}</code><br>"
                f"<b>By&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</b> : {by}<br>"
                f"<b>Error type</b> : {type(captured_error).__name__}<br>"
                f"<b>Error msg&nbsp;</b> : {str(captured_error)[:300]}"
                f"</div>",
                "WARN", html=True)

            # ── STEP 9: Capture page source ─────────────────────────────────
            try:
                page_source = driver_self.page_source
                listener_ref._log(
                    f"[SELF-HEAL] Page source captured — {len(page_source):,} chars "
                    f"for {var_name}", "INFO")
            except Exception as ps_exc:
                listener_ref._log(
                    f"[SELF-HEAL] ✗ Cannot capture page source for {var_name}: "
                    f"{ps_exc}. Aborting heal.", "WARN")
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 10: Run healer logic ───────────────────────────────────
            try:
                new_locator = find_healed_locator(value_str, page_source)
            except Exception:
                listener_ref._log(
                    f"[SELF-HEAL] ✗ HealerLogic exception for {var_name}:<br>"
                    f"<pre>{traceback.format_exc()}</pre>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 11: No match found ─────────────────────────────────────
            if not new_locator:
                msg = (
                    f"<div style='border:2px solid #cc0000;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#fff0f0'>"
                    f"<b style='color:#cc0000;font-size:13px'>"
                    f"✗ NO MATCHING XPATH FOUND</b><br><br>"
                    f"<b>Variable&nbsp;</b> : <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>XPath&nbsp;&nbsp;&nbsp;&nbsp;</b> : <code>{value_str}</code><br>"
                    f"<b>Fail count</b> : {fail_count}<br><br>"
                    f"<i>Possible causes:</i><br>"
                    f"&bull; All tokens in the locator are too short (&lt;4 chars) "
                    f"or in the generic-words blocklist<br>"
                    f"&bull; Element genuinely does not exist on this screen<br>"
                    f"&bull; App is mid-transition / screen not fully loaded<br>"
                    f"&bull; resource-id was renamed with no similar ID in the current XML"
                    f"</div>"
                )
                listener_ref._console(
                    f"{_C_RED}{_C_BOLD}[SELF-HEAL] ✗ NO MATCH  "
                    f"var={var_name}{_C_RESET}")
                listener_ref._log(msg, "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 12: Try the healed locator ────────────────────────────
            healed_xpath = (
                new_locator[len("xpath="):]
                if new_locator.startswith("xpath=")
                else new_locator
            )

            listener_ref._console(
                f"{_C_CYAN}[SELF-HEAL] 🔍 Candidate found for {var_name}: "
                f"{healed_xpath[:80]}{_C_RESET}")
            listener_ref._log(
                f"<div style='border:2px solid #0077cc;border-radius:4px;"
                f"padding:6px 10px;margin:4px 0;background:#f0f8ff'>"
                f"<b style='color:#0077cc'>🔍 HEAL CANDIDATE FOUND — retrying...</b><br><br>"
                f"<b>Variable&nbsp;</b> : <code style='color:#9b59b6'>{var_name}</code><br>"
                f"<b>Original&nbsp;</b> : <code style='color:#cc0000'>{value_str}</code><br>"
                f"<b>Healed&nbsp;&nbsp;&nbsp;</b> : <code style='color:#007700'>{healed_xpath}</code>"
                f"</div>",
                "INFO", html=True)

            try:
                found = original_find_element(driver_self, by=By.XPATH, value=healed_xpath)

                listener_ref._console(
                    f"{_C_GREEN}{_C_BOLD}[SELF-HEAL] ✓ SUCCESS  "
                    f"var={var_name}  healed={healed_xpath[:80]}{_C_RESET}")
                listener_ref._log(
                    f"<div style='border:2px solid #007700;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#f0fff0'>"
                    f"<b style='color:#007700;font-size:13px'>✓ SELF-HEAL SUCCESS</b><br><br>"
                    f"<b>Variable&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</b> : "
                    f"<code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>Original XPath&nbsp;&nbsp;&nbsp;</b> : "
                    f"<code style='color:#cc0000'>{value_str}</code><br>"
                    f"<b>Healed XPath&nbsp;&nbsp;&nbsp;&nbsp;</b> : "
                    f"<code style='color:#007700'>{healed_xpath}</code><br>"
                    f"<b>Failures before heal</b> : {fail_count}"
                    f"</div>",
                    "INFO", html=True)

                failure_counts[value_str] = 0   # reset counter on success
                return found

            except Exception as retry_exc:
                listener_ref._console(
                    f"{_C_RED}{_C_BOLD}[SELF-HEAL] ✗ HEALED LOCATOR ALSO FAILED  "
                    f"var={var_name}{_C_RESET}")
                listener_ref._log(
                    f"<div style='border:2px solid #cc0000;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#fff0f0'>"
                    f"<b style='color:#cc0000'>✗ HEALED LOCATOR ALSO FAILED</b><br><br>"
                    f"<b>Variable&nbsp;</b> : <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>Tried&nbsp;&nbsp;&nbsp;&nbsp;</b> : <code>{healed_xpath}</code><br>"
                    f"<b>Error&nbsp;&nbsp;&nbsp;&nbsp;</b> : "
                    f"{type(retry_exc).__name__}: {str(retry_exc)[:200]}<br>"
                    f"<i>Marked unhealable for {listener_ref.UNHEALABLE_TTL:.0f}s</i>"
                    f"</div>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

        # ── Install the patch ────────────────────────────────────────────────
        healed_find_element._is_healed = True
        WebDriver.find_element = healed_find_element
        self.webdriver_patched = True
        self._console(
            f"{_C_GREEN}{_C_BOLD}[SELF-HEAL] ⚡ ENGINE ACTIVE — "
            f"WebDriver.find_element patched globally{_C_RESET}")
        self._log(
            "<b style='color:green'>⚡ SELF-HEAL ENGINE ACTIVE — "
            "WebDriver.find_element patched globally.</b>",
            "INFO", html=True)

    # ────────────────────────────────────────────────────────────────────────
    # Logging helpers
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _log(message: str, level: str = "INFO", html: bool = False):
        """Write to Robot Framework log (report). Never raises."""
        try:
            BuiltIn().log(message, level, html=html)
        except Exception:
            pass

    @staticmethod
    def _console(message: str):
        """Write a plain-text line to the console/terminal. Never raises."""
        try:
            print(f"\n{message}", flush=True)
        except Exception:
            pass
