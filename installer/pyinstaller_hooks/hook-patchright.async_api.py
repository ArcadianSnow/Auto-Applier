# PyInstaller hook: bundle patchright's node driver.
#
# patchright forked playwright but kept the UPSTREAM hook filenames — it ships
# `_impl/__pyinstaller/hook-playwright.async_api.py`, which PyInstaller only fires for an
# import of `playwright.async_api`. Our code imports `patchright.async_api`, so that hook
# never runs and `patchright/driver/` (node.exe + package/cli.js) is left out of the bundle.
# Upstream closed the report as wontfix.
#
# Measured 2026-07-31 on a probe exe built exactly like build.py:
#   without this hook -> driver dir present: False -> BrowserType.launch raises
#                        FileNotFoundError [WinError 2] (it's trying to exec a node.exe
#                        that isn't there). Every browser path in the shipped app is dead.
#   with it           -> driver present, node.exe present, browser launches OK.
#
# Three lines, and it's the difference between a shippable exe and one that can't apply.
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("patchright")
