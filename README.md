# Appium Runtime Self-Healing Mechanism POC
This directory contains a **Proof of Concept (POC)** for a self-healing automation system using Robot Framework and Appium.

## How it Works Native to Docker
Since your repository already handles the heavy lifting of connection and Appium setup during Docker initialization, **you don't sub out any work to external APIs**, and you **don't modify your existing `.robot` files.**

We use a **Robot Framework `--listener`** to silently intercept Appium errors inside the same Docker process.

1. You run tests just like normal, but add the listener flag: `robot --listener /path/to/self_heal/SelfHealingListener.py my_test.robot`
2. Whenever `AppiumLibrary` throws an "Element Not Found" error, the listener intercepts it in-memory.
3. It captures the full XML of the Appium Screen (`AppiumLibrary.get_source()`).
4. It parses that XML *locally using pure Python* (`HealerLogic.py`) to find a heavily matching element (e.g. searching for attributes that match your old broken ID strings).
5. If it finds the new button, it transparently retries the Appium Action. Your test never fails!

## Files
- **`SelfHealingListener.py`**: The Global Listener. It dynamically patches AppiumLibrary without test modifications.
- **`HealerLogic.py`**: The Local Python Logic. Uses simple `xml.etree.ElementTree` to compare broken locator strings against current screen nodes.
- **`sample_test.robot`**: An unmodified standard test. It knows nothing about self-healing, but will heal itself when run with the listener.
- **`healed_locators.json`**: An auto-generated file that caches healed locators natively so it speeds up future test runs.


Sample run cmd
robot --listener libs/SelfHealingListener.py -d results -v ENV:qa tests/sample_suite.robot



1. Where exactly in the code handles the logic?
If you look at 

HealerLogic.py
 (which you currently have open), here are the exact lines:

Line 31: keywords = value.lower().replace('_', ' ').replace('-', ' ').replace('/', ' ').split() (This is where it strips out the symbols and breaks id=submit_btn_old into words)

Line 33: for node in root.iter(): (This starts the loop that scans through every single XML element on your App screen)

Line 41: for kw in keywords:

Line 42: if len(kw) > 3 and (kw in text or kw in content_desc): (This is the exact line where it checks if the keyword exists inside the element's text or description. If this is True, it considers the element "found".)