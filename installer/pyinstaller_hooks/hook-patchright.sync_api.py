# Sibling of hook-patchright.async_api.py — see that file for the full rationale.
# Present so the driver is bundled whichever API surface an entry point happens to import.
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("patchright")
