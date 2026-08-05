"""Guards on the Commit Design System rules that are easy to break by accident.

Offline source scanning, in the same spirit as `test_ui_boundaries.py` — these
check the *source*, not rendered output, so they need neither a browser nor a
running app.

Only the mechanically checkable rules live here. "Sentence case", "confident,
warm, direct", and "no gradients beyond the pattern's vignette" are real brand
rules too, but a regex would either miss them or produce false positives, so
they stay a review concern rather than a fake test.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "src" / "wdmigrator" / "ui"
STATIC = ROOT / "static"

#: Emoji and the dingbats most likely to be reached for as icons. The brand
#: forbids both — the cyan checkmark (`theme.CHECK_SVG`) is the only marker
#: glyph, and status is carried by color plus a top border rule.
#:
#: Box-drawing characters are deliberately NOT in this range: `─` is used in
#: comment section rules throughout the codebase and never reaches a user.
_BANNED = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji blocks
    "←-⇿"          # arrows
    "☀-➿"          # misc symbols + dingbats
    "⬀-⯿"          # more arrows/symbols
    "️"                 # variation selector-16 (emoji presentation)
    "]"
)


def _ui_sources() -> list[pathlib.Path]:
    return sorted(UI_ROOT.rglob("*.py"))


@pytest.mark.parametrize("path", _ui_sources(), ids=lambda p: p.name)
def test_no_emoji_or_dingbats_in_ui_source(path: pathlib.Path):
    offenders = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in _BANNED.finditer(line):
            offenders.append(f"{path.name}:{i} contains {match.group()!r}")
    assert not offenders, (
        "The Commit brand forbids emoji and dingbat icons. Use theme.banner() "
        "for status and theme.CHECK_SVG for markers:\n" + "\n".join(offenders)
    )


def test_ui_renders_status_through_theme_not_streamlit_defaults():
    """`st.success`/`st.error`/`st.warning`/`st.info` ship emoji icons by
    default, so the wizard routes status through `theme.banner` instead."""
    offenders = []
    pattern = re.compile(r"\bst\.(success|error|warning|info)\(")
    for path in _ui_sources():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, (
        "Use theme.banner(kind, ...) rather than Streamlit's own status calls:\n"
        + "\n".join(offenders)
    )


def test_only_real_open_sans_weights_are_used():
    """Only Regular (400) and Bold (700) ship as font files. A CSS rule asking
    for 500/600/800 gets a browser-synthesized faux-bold that is not Open Sans."""
    css = (UI_ROOT / "theme.py").read_text(encoding="utf-8")
    weights = set(re.findall(r"font-weight:\s*(\d{3})", css))
    assert weights <= {"400", "700"}, f"synthesized weights in use: {sorted(weights - {'400', '700'})}"


def test_shadows_are_navy_tinted_never_gray():
    css = (UI_ROOT / "theme.py").read_text(encoding="utf-8")
    for shadow in re.findall(r"--shadow-[a-z]+:\s*([^;]+);", css):
        assert "rgba(10,17,64" in shadow or "rgba(0,174,239" in shadow, (
            f"shadow is not navy- or accent-tinted: {shadow.strip()}"
        )


def test_no_left_border_stripe_cards():
    """Explicitly not in the brand vocabulary — accent goes inside the card or
    on its top edge."""
    css = (UI_ROOT / "theme.py").read_text(encoding="utf-8")
    assert not re.search(r"border-left:\s*[3-9]px", css)


def test_brand_assets_are_present_and_local():
    """Every asset is served from ./static — the app makes no CDN request, which
    matters on the locked-down networks these tenants tend to sit behind."""
    for name in (
        "logo-commit-white.png",
        "logo-minimal.png",
        "bg-blue-pattern-header.png",
        "fonts/OpenSans-Regular.ttf",
        "fonts/OpenSans-Bold.ttf",
        "fonts/OpenSans-Italic.ttf",
        "fonts/OpenSans-BoldItalic.ttf",
    ):
        assert (STATIC / name).is_file(), f"missing brand asset: static/{name}"


def test_theme_css_has_no_external_urls():
    css = (UI_ROOT / "theme.py").read_text(encoding="utf-8")
    assert "http://" not in css
    assert "https://" not in css


def test_static_serving_and_fonts_are_configured():
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert "enableStaticServing = true" in config
    assert config.count("[[theme.fontFaces]]") == 4, "all four real Open Sans files must be registered"
    # Brand rule: default text is Blue Black, never pure black.
    assert '"#0A1140"' in config
    assert '"#000000"' not in config
