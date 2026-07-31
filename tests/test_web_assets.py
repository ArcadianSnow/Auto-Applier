"""The dashboard must not depend on the network to render.

Alpine.js drives every interactive element on every page, and it was loaded from
`cdn.jsdelivr.net`. A blocked or unreachable CDN therefore didn't *degrade* the dashboard — it
killed it outright, with no error a non-technical user could act on. That's a real failure mode
for the audience (a tester on a locked-down work laptop, behind an ad-blocker, or offline), and
it contradicted the local-first promise the footer prints on every page.

These tests fail if a template starts pulling a script or stylesheet from an external origin
again. Ordinary `<a href>` links out (e.g. "Get Ollama") are fine — those are navigation the
user chooses, not a render-blocking dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "auto_applier" / "web" / "templates"
STATIC = Path(__file__).resolve().parent.parent / "auto_applier" / "web" / "static"

#: src=/href= on a loading tag pointing at an absolute external origin.
_EXTERNAL_ASSET = re.compile(
    r"<(?:script|link)\b[^>]*?(?:src|href)\s*=\s*[\"'](?P<url>(?:https?:)?//[^\"']+)",
    re.IGNORECASE,
)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def test_templates_exist():
    assert _templates(), f"no templates under {TEMPLATES}"


@pytest.mark.parametrize("tpl", _templates(), ids=lambda p: p.name)
def test_no_template_loads_a_script_or_stylesheet_from_the_network(tpl: Path):
    hits = [m.group("url") for m in _EXTERNAL_ASSET.finditer(tpl.read_text(encoding="utf-8"))]
    assert not hits, (
        f"{tpl.name} loads {hits} from an external origin -> the page stops working when that "
        f"host is blocked or unreachable. Vendor it under web/static/vendor/ instead "
        f"(see that dir's README)."
    )


def test_alpine_is_vendored_and_served_locally():
    vendored = STATIC / "vendor" / "alpine-3.14.1.min.js"
    assert vendored.is_file(), f"missing {vendored}"
    assert vendored.stat().st_size > 20_000, "vendored Alpine looks truncated"
    assert "Alpine" in vendored.read_text(encoding="utf-8", errors="ignore")

    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "vendor/alpine-3.14.1.min.js" in base
    assert "cdn.jsdelivr.net" not in base


def test_vendor_dir_documents_what_it_ships():
    """Third-party code in the tree needs its version + license recorded."""
    readme = (STATIC / "vendor" / "README.md").read_text(encoding="utf-8")
    assert "alpine-3.14.1.min.js" in readme
    assert "MIT" in readme
