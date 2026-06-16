"""Bundled data resources (read via ``importlib.resources``).

Currently holds ``ats_companies.csv`` — the offline company→ATS→slug directory the
``seed-boards`` flow probes (see ``auto_applier/sources/ats_directory.py``). Shipping it
as package data keeps slug seeding zero-runtime-egress; the only network in seeding is the
confirm-probe against the same public ATS read APIs discovery already uses.
"""
