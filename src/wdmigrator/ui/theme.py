"""The Commit Design System, applied to Streamlit.

Two layers, and the split matters:

1. **``.streamlit/config.toml``'s ``[theme]`` block** sets the native tokens —
   colors, radius, and the four real Open Sans font files. Streamlit's own
   chrome (widgets, focus rings, the top bar) is brand-correct from that alone,
   with no CSS involved. If everything in *this* module were deleted, the app
   would still be recognisably Commit rather than default-Streamlit purple.
2. **:func:`inject`'s stylesheet plus the primitives below** cover what native
   theming cannot reach: the header band, the step rail, status banners, the
   cyan checkmark as a bullet glyph.

Layer 2 targets Streamlit internals by ``data-testid``, which is stable across
minor versions in a way the generated ``.st-emotion-cache-*`` class names are
emphatically not — never write a selector against those. Anything here that
silently stops matching after a Streamlit upgrade degrades to layer 1, which is
why layer 1 carries the brand-critical values and layer 2 only refines them.

The brand rules this module is written to obey (see the design system's
README — violating any one reads as off-brand immediately):

- Open Sans only, and only weights **400 and 700** — those are the only real
  font files that ship. Asking for 500/600/800 gets a browser-synthesized
  faux-bold that is not Open Sans.
- Default text is ``--commit-blue-black``, never pure black. Shadows are
  navy-tinted, never gray.
- **No emoji, and no Unicode dingbats standing in for icons.** The cyan
  checkmark (:data:`CHECK_SVG`) is the signature marker glyph; status uses
  color, weight, and a top border rule instead of a pictogram.
- Sentence case for every heading and button. ALL-CAPS only for short eyebrow
  labels, at ``--tracking-eyebrow``.
- No gradients beyond the vignette already baked into the background pattern,
  and that pattern is for the header band only — never behind body content.
- No cards with a colored left-border stripe. Accent goes *inside* the card, or
  on its top edge.
- Motion is ``--ease-out`` at 150/250ms. No bounce, no spring.
"""

from __future__ import annotations

import html
from typing import Iterable, Mapping, Sequence

import streamlit as st

from wdmigrator.api import Environment

#: Static-served brand assets. `enableStaticServing = true` in
#: `.streamlit/config.toml` maps ./static onto this prefix. Everything is
#: local — the app makes no CDN request for a font, an icon, or an image.
_STATIC = "app/static"
LOGO_REVERSE = f"{_STATIC}/logo-commit-white.png"
HEADER_PATTERN = f"{_STATIC}/bg-blue-pattern-header.png"
FAVICON_PATH = "static/logo-minimal.png"

#: The one branded glyph: the cyan checkmark from inside the logo's circular C.
#: Inlined rather than fetched so it can inherit sizing from its container and
#: cost nothing to repeat as a list marker.
CHECK_SVG = (
    '<svg class="cmt-check" viewBox="0 0 48 48" fill="none" stroke="currentColor" '
    'stroke-width="6" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">'
    '<path d="M8 25 L20 36 L42 10"></path></svg>'
)

# Brand values needed in Python (Streamlit APIs that take a color string, not
# CSS). The CSS below defines the same values as custom properties; these
# constants exist for the handful of call sites that cannot use var().
COBALT_BLUE = "#29357F"
ACCENT_BLUE = "#00AEEF"
BLUE_BLACK = "#0A1140"

#: Which semantic color a tenant environment reads as. UNKNOWN is deliberately
#: styled identically to PRODUCTION: `safety.py` treats them the same, and a
#: badge that looked softer than the guard behaves would be a lie.
_ENV_TONE: Mapping[Environment, str] = {
    Environment.IMPLEMENTATION: "safe",
    Environment.SANDBOX: "safe",
    Environment.PRODUCTION: "danger",
    Environment.UNKNOWN: "danger",
}

_ENV_LABEL: Mapping[Environment, str] = {
    Environment.IMPLEMENTATION: "Implementation",
    Environment.SANDBOX: "Sandbox",
    Environment.PRODUCTION: "Production",
    Environment.UNKNOWN: "Unknown environment",
}

_STYLESHEET = f"""
<style>
:root {{
  /* Brand palette — exact values from the Commit Brand Guidelines. */
  --commit-cobalt-blue: #29357F;
  --commit-dark-blue: #182366;
  --commit-blue-black: #0A1140;
  --commit-accent-blue: #00AEEF;
  --commit-bright-gold: #FBA528;
  --commit-dark-gray: #353535;
  --commit-light-gray: #F2F2F2;
  --commit-surface-tint: #EEF1FA;
  --commit-border-soft: #D9DDE9;
  --commit-cobalt-blue-hover: #1F2A6A;
  --commit-cobalt-blue-press: #161F52;

  /* Semantic aliases — prefer these over the raw brand tokens. */
  --fg-default: var(--commit-blue-black);
  --fg-strong: var(--commit-dark-blue);
  --fg-muted: var(--commit-dark-gray);
  --fg-subtle: #6A6F85;
  --fg-on-dark: #FFFFFF;
  --fg-accent: var(--commit-accent-blue);
  --bg-page: #FFFFFF;
  --bg-tint: var(--commit-surface-tint);
  --bg-dark: var(--commit-dark-blue);
  --border-default: var(--commit-border-soft);

  --status-success: #1F8A5B;
  --status-warning: var(--commit-bright-gold);
  --status-danger: #C8362B;
  --status-info: var(--commit-accent-blue);

  /* Only 400 and 700 exist as real font files — see the module docstring. */
  --fw-regular: 400;
  --fw-bold: 700;

  --fs-h1: 44px;
  --fs-h2: 32px;
  --fs-h3: 24px;
  --fs-h4: 20px;
  --fs-body: 16px;
  --fs-body-sm: 14px;
  --fs-caption: 12px;
  --fs-eyebrow: 11px;

  --tracking-tight: -0.01em;
  --tracking-eyebrow: 0.12em;

  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;
  --radius-pill: 999px;

  /* Navy-tinted, never gray. */
  --shadow-xs: 0 1px 2px rgba(10,17,64,0.3);
  --shadow-sm: 0 2px 4px rgba(10,17,64,0.3);
  --shadow-md: 0 4px 12px rgba(10,17,64,0.3);
  --shadow-focus: 0 0 0 3px rgba(0,174,239,0.35);

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;

  --ease-out: cubic-bezier(0.2, 0, 0, 1);
  --duration-fast: 150ms;
  --duration-ui: 250ms;
}}

/* ── Base ─────────────────────────────────────────────────────────────── */

html, body, [data-testid="stAppViewContainer"] {{
  font-family: "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color: var(--fg-default);
}}

[data-testid="stMain"] .block-container {{
  padding-top: var(--space-6);
  padding-bottom: var(--space-8);
  max-width: 1200px;
}}

/* Streamlit's own top bar would otherwise float a white strip over the
   branded header band. */
[data-testid="stHeader"] {{ background: transparent; }}

h1, h2, h3, h4, h5, h6 {{
  color: var(--fg-strong);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-tight);
}}

[data-testid="stMain"] h2 {{ font-size: var(--fs-h2); }}
[data-testid="stMain"] h3 {{ font-size: var(--fs-h3); }}

hr, [data-testid="stDivider"] hr {{
  border-color: var(--border-default);
  opacity: 1;
}}

code, kbd, pre {{ color: var(--fg-strong); }}

/* ── Header band ──────────────────────────────────────────────────────── */
/* The signature dotted-pixel pattern, used the one way the brand allows it:
   as a header/cover, with the white reverse logo, never behind body content.
   The navy overlay is the prescribed rgba(10,17,64,0.5-0.7) so text on the
   pattern keeps its contrast. */

.cmt-header {{
  position: relative;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background-color: var(--bg-dark);
  background-image: linear-gradient(rgba(10,17,64,0.62), rgba(10,17,64,0.62)),
                    url("{HEADER_PATTERN}");
  background-size: cover;
  background-position: center;
  padding: var(--space-6) var(--space-8);
  margin-bottom: var(--space-6);
  box-shadow: var(--shadow-sm);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-8);
  flex-wrap: wrap;
}}

.cmt-header__brand {{ display: flex; align-items: center; gap: var(--space-5); }}
.cmt-header__logo {{ height: 34px; width: auto; display: block; }}
.cmt-header__rule {{
  width: 1px; height: 42px;
  background: rgba(255,255,255,0.28);
}}
.cmt-header__eyebrow {{
  font-size: var(--fs-eyebrow);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-eyebrow);
  text-transform: uppercase;
  color: var(--commit-accent-blue);
  margin-bottom: var(--space-1);
}}
.cmt-header__title {{
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-tight);
  color: var(--fg-on-dark);
  line-height: 1.15;
}}

.cmt-route {{ display: flex; align-items: center; gap: var(--space-4); }}
.cmt-route__leg {{ min-width: 0; }}
.cmt-route__label {{
  font-size: var(--fs-eyebrow);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-eyebrow);
  text-transform: uppercase;
  color: rgba(255,255,255,0.62);
  margin-bottom: var(--space-1);
}}
.cmt-route__tenant {{
  font-size: var(--fs-body-sm);
  font-weight: var(--fw-bold);
  color: var(--fg-on-dark);
  white-space: nowrap;
}}
.cmt-route__arrow {{
  color: var(--commit-accent-blue);
  flex: none;
}}
.cmt-route__arrow svg {{ width: 22px; height: 12px; display: block; }}

/* ── Pills and badges ─────────────────────────────────────────────────── */

.cmt-pill {{
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border-radius: var(--radius-pill);
  padding: 2px 10px;
  font-size: var(--fs-caption);
  font-weight: var(--fw-bold);
  letter-spacing: 0.02em;
  white-space: nowrap;
  margin-top: var(--space-1);
}}
.cmt-pill__dot {{ width: 6px; height: 6px; border-radius: var(--radius-pill); flex: none; }}
.cmt-pill--safe {{ background: rgba(0,174,239,0.16); color: #0E4E63; }}
.cmt-pill--safe .cmt-pill__dot {{ background: var(--commit-accent-blue); }}
.cmt-pill--danger {{ background: rgba(200,54,43,0.12); color: var(--status-danger); }}
.cmt-pill--danger .cmt-pill__dot {{ background: var(--status-danger); }}
.cmt-pill--neutral {{ background: var(--bg-tint); color: var(--fg-muted); }}
.cmt-pill--neutral .cmt-pill__dot {{ background: var(--fg-subtle); }}

/* On the dark header band the light pill fills read as muddy — invert. */
.cmt-header .cmt-pill--safe {{ background: rgba(0,174,239,0.20); color: #7FDCFF; }}
.cmt-header .cmt-pill--danger {{ background: rgba(200,54,43,0.24); color: #FFB4AD; }}
.cmt-header .cmt-pill--neutral {{ background: rgba(255,255,255,0.14); color: rgba(255,255,255,0.78); }}

/* ── Step rail ────────────────────────────────────────────────────────── */
/* Read-only progress display, never a nav control: jumping straight to
   Execute is exactly what ui/app.py's gating exists to prevent, so nothing
   here is clickable. */

.cmt-steps {{
  display: flex;
  align-items: flex-start;
  margin: 0 0 var(--space-5) 0;
  padding: 0;
  list-style: none;
}}
.cmt-step {{ flex: 1; text-align: center; position: relative; min-width: 0; }}
.cmt-step__mark {{
  width: 30px; height: 30px;
  border-radius: var(--radius-pill);
  display: flex; align-items: center; justify-content: center;
  margin: 0 auto var(--space-2) auto;
  font-size: var(--fs-caption);
  font-weight: var(--fw-bold);
  border: 1px solid var(--border-default);
  background: var(--bg-page);
  color: var(--fg-subtle);
  position: relative; z-index: 1;
  transition: background var(--duration-fast) var(--ease-out),
              color var(--duration-fast) var(--ease-out),
              border-color var(--duration-fast) var(--ease-out);
}}
.cmt-step__label {{
  font-size: var(--fs-body-sm);
  color: var(--fg-subtle);
  letter-spacing: 0;
}}
/* The connecting rail, drawn behind the marks. */
.cmt-step::before {{
  content: "";
  position: absolute;
  top: 15px; left: -50%; width: 100%;
  height: 1px;
  background: var(--border-default);
  z-index: 0;
}}
.cmt-step:first-child::before {{ display: none; }}

.cmt-step--done .cmt-step__mark {{
  background: var(--commit-cobalt-blue);
  border-color: var(--commit-cobalt-blue);
  color: var(--commit-accent-blue);
}}
.cmt-step--done .cmt-step__label {{ color: var(--fg-muted); }}
.cmt-step--done::before {{ background: var(--commit-cobalt-blue); }}

.cmt-step--current .cmt-step__mark {{
  background: var(--commit-cobalt-blue);
  border-color: var(--commit-cobalt-blue);
  color: var(--fg-on-dark);
  box-shadow: var(--shadow-focus);
}}
.cmt-step--current .cmt-step__label {{ color: var(--fg-strong); font-weight: var(--fw-bold); }}

.cmt-step__mark .cmt-check {{ width: 14px; height: 14px; }}

/* ── Section headings ─────────────────────────────────────────────────── */

.cmt-eyebrow {{
  font-size: var(--fs-eyebrow);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-eyebrow);
  text-transform: uppercase;
  color: var(--commit-cobalt-blue);
  margin-bottom: var(--space-2);
}}
.cmt-section {{ margin: var(--space-2) 0 var(--space-4) 0; }}
.cmt-section__title {{
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-tight);
  color: var(--fg-strong);
  line-height: 1.2;
}}
.cmt-section__caption {{
  font-size: var(--fs-body-sm);
  color: var(--fg-muted);
  margin-top: var(--space-2);
  max-width: 76ch;
}}

/* ── Cards ────────────────────────────────────────────────────────────── */
/* White, 1px soft border, small navy shadow. Accent lives inside the card or
   on its top edge — never as a left-border stripe. */

.cmt-card {{
  background: var(--bg-page);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-3);
}}
.cmt-card__title {{
  font-size: var(--fs-body);
  font-weight: var(--fw-bold);
  color: var(--fg-strong);
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--space-3); flex-wrap: wrap;
}}
.cmt-card__meta {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-caption);
  color: var(--fg-muted);
  margin-top: var(--space-2);
  word-break: break-all;
}}
.cmt-card__note {{
  font-size: var(--fs-caption);
  color: var(--fg-muted);
  margin-top: var(--space-2);
}}

/* ── Status banners ───────────────────────────────────────────────────── */
/* Color plus a top accent rule carries the state — no pictogram, per the
   no-emoji rule. */

.cmt-banner {{
  border: 1px solid var(--border-default);
  border-top: 3px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-3);
  background: var(--bg-page);
  box-shadow: var(--shadow-xs);
}}
.cmt-banner__title {{
  font-size: var(--fs-body-sm);
  font-weight: var(--fw-bold);
  color: var(--fg-strong);
  display: flex; align-items: baseline; gap: var(--space-2);
}}
.cmt-banner__body {{
  font-size: var(--fs-body-sm);
  color: var(--fg-muted);
  margin-top: var(--space-2);
}}
.cmt-banner__remedy {{
  font-size: var(--fs-body-sm);
  color: var(--fg-default);
  margin-top: var(--space-2);
}}
.cmt-banner__remedy strong {{ color: var(--fg-strong); }}
.cmt-banner__where {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-caption);
  font-weight: var(--fw-regular);
  color: var(--fg-subtle);
}}

.cmt-banner--success {{ border-top-color: var(--status-success); background: rgba(31,138,91,0.05); }}
.cmt-banner--success .cmt-banner__title {{ color: var(--status-success); }}
.cmt-banner--info {{ border-top-color: var(--commit-accent-blue); background: rgba(0,174,239,0.06); }}
.cmt-banner--info .cmt-banner__title {{ color: #0E4E63; }}
.cmt-banner--warning {{ border-top-color: var(--status-warning); background: rgba(251,165,40,0.08); }}
.cmt-banner--warning .cmt-banner__title {{ color: #8A5406; }}
.cmt-banner--danger {{ border-top-color: var(--status-danger); background: rgba(200,54,43,0.06); }}
.cmt-banner--danger .cmt-banner__title {{ color: var(--status-danger); }}
.cmt-banner--neutral {{ background: var(--bg-tint); }}
.cmt-banner--neutral .cmt-banner__title {{ color: var(--fg-strong); }}

/* ── Checkmark bullets — the signature marker glyph ───────────────────── */

.cmt-check {{ width: 12px; height: 12px; flex: none; }}
.cmt-checklist {{ list-style: none; margin: var(--space-2) 0; padding: 0; }}
.cmt-checklist li {{
  display: flex; align-items: baseline; gap: var(--space-3);
  font-size: var(--fs-body-sm);
  color: var(--fg-default);
  margin-bottom: var(--space-2);
}}
.cmt-checklist li .cmt-check {{ color: var(--commit-accent-blue); position: relative; top: 1px; }}

/* ── Figures ──────────────────────────────────────────────────────────── */

.cmt-figures {{ display: flex; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-3); }}
.cmt-figure {{
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-page);
  box-shadow: var(--shadow-xs);
  padding: var(--space-3) var(--space-5);
  min-width: 116px;
}}
.cmt-figure__value {{
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  color: var(--commit-cobalt-blue);
  letter-spacing: var(--tracking-tight);
  line-height: 1.1;
}}
.cmt-figure__label {{
  font-size: var(--fs-eyebrow);
  font-weight: var(--fw-bold);
  letter-spacing: var(--tracking-eyebrow);
  text-transform: uppercase;
  color: var(--fg-subtle);
  margin-top: var(--space-1);
}}
/* Gold is the brand's "key data point" accent — reserved here for the count
   that actually changes a tenant. */
.cmt-figure--write .cmt-figure__value {{ color: var(--commit-bright-gold); }}
.cmt-figure--danger .cmt-figure__value {{ color: var(--status-danger); }}
.cmt-figure--muted .cmt-figure__value {{ color: var(--fg-subtle); }}

/* ── Streamlit widget refinements ─────────────────────────────────────── */

.stButton button, .stDownloadButton button, .stFormSubmitButton button {{
  border-radius: var(--radius-md);
  font-weight: var(--fw-bold);
  letter-spacing: 0;
  transition: background var(--duration-fast) var(--ease-out),
              border-color var(--duration-fast) var(--ease-out),
              transform var(--duration-fast) var(--ease-out);
}}
/* Press: darken and scale 0.98 — never invert, never bounce. */
.stButton button:active, .stDownloadButton button:active, .stFormSubmitButton button:active {{
  transform: scale(0.98);
}}
.stButton button[kind="primary"] {{
  background: var(--commit-cobalt-blue);
  border-color: var(--commit-cobalt-blue);
  color: var(--fg-on-dark);
}}
.stButton button[kind="primary"]:hover {{
  background: var(--commit-cobalt-blue-hover);
  border-color: var(--commit-cobalt-blue-hover);
}}
.stButton button:focus-visible, .stDownloadButton button:focus-visible {{
  box-shadow: var(--shadow-focus);
}}
.stButton button:disabled {{ opacity: 0.45; }}

[data-testid="stExpander"] details {{
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-page);
  box-shadow: var(--shadow-xs);
}}
[data-testid="stExpander"] summary {{ font-size: var(--fs-body-sm); font-weight: var(--fw-bold); color: var(--fg-strong); }}

[data-testid="stProgress"] div[role="progressbar"] > div > div {{
  background: var(--commit-accent-blue);
}}

[data-testid="stTextInput"] input:focus {{ box-shadow: var(--shadow-focus); }}

[data-testid="stDataFrame"] {{
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  overflow: hidden;
}}

[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{
  color: var(--fg-muted);
  font-size: var(--fs-body-sm);
}}
</style>
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def inject() -> None:
    """Apply the stylesheet. Call on **every** run, right after ``set_page_config``.

    Deliberately not guarded by a "already injected" session flag. Streamlit
    reconciles the DOM by element position and *removes* anything a run does
    not re-produce, so a guard that emits the ``<style>`` block only on the
    first run loses it again on the very next interaction — the app renders
    correctly once and then falls back to bare Streamlit chrome. (Confirmed in
    the browser; the guard was tried first and did exactly this.) Re-emitting
    does not stack duplicate nodes for the same reason: the element is
    replaced in place, not appended.
    """
    st.markdown(_STYLESHEET, unsafe_allow_html=True)


def env_pill(environment: Environment | None) -> str:
    """The environment badge, as an HTML fragment for embedding."""
    if environment is None:
        return '<span class="cmt-pill cmt-pill--neutral"><span class="cmt-pill__dot"></span>Not set</span>'
    tone = _ENV_TONE.get(environment, "danger")
    label = _ENV_LABEL.get(environment, str(environment))
    return (
        f'<span class="cmt-pill cmt-pill--{tone}"><span class="cmt-pill__dot"></span>{_esc(label)}</span>'
    )


def page_header(source_tenant: str | None, source_env: Environment | None,
                dest_tenant: str | None, dest_env: Environment | None) -> None:
    """The branded header band, carrying the run's source-to-destination route.

    Keeping the route visible on every step is the point: this app writes
    irreversibly to whichever tenant is on the right-hand side, so "which
    tenant am I pointed at" should never require navigating back to Connect.
    """
    def leg(label: str, tenant: str | None, environment: Environment | None) -> str:
        name = _esc(tenant) if tenant else "Not connected"
        return (
            f'<div class="cmt-route__leg"><div class="cmt-route__label">{_esc(label)}</div>'
            f'<div class="cmt-route__tenant">{name}</div>{env_pill(environment)}</div>'
        )

    arrow = (
        '<div class="cmt-route__arrow"><svg viewBox="0 0 44 24" fill="none" stroke="currentColor" '
        'stroke-width="3" stroke-linecap="square" aria-hidden="true">'
        '<path d="M2 12 H38 M30 4 L38 12 L30 20"></path></svg></div>'
    )

    st.markdown(
        '<div class="cmt-header">'
        '<div class="cmt-header__brand">'
        f'<img class="cmt-header__logo" src="{LOGO_REVERSE}" alt="Commit Consulting">'
        '<div class="cmt-header__rule"></div>'
        '<div><div class="cmt-header__eyebrow">Workday tenant tooling</div>'
        '<div class="cmt-header__title">Configuration migrator</div></div>'
        '</div>'
        f'<div class="cmt-route">{leg("Source", source_tenant, source_env)}{arrow}'
        f'{leg("Destination", dest_tenant, dest_env)}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def stepper(current: str, order: Sequence[str], titles: Mapping[str, str],
            unlocked_through: int) -> None:
    """The step rail. Read-only by construction — see the CSS comment."""
    items = []
    for i, step_id in enumerate(order):
        if step_id == current:
            state, mark = "current", str(i + 1)
        elif i <= unlocked_through:
            state, mark = "done", CHECK_SVG
        else:
            state, mark = "locked", str(i + 1)
        items.append(
            f'<li class="cmt-step cmt-step--{state}">'
            f'<div class="cmt-step__mark">{mark}</div>'
            f'<div class="cmt-step__label">{_esc(titles[step_id])}</div></li>'
        )
    st.markdown(f'<ul class="cmt-steps">{"".join(items)}</ul>', unsafe_allow_html=True)


def section(title: str, caption: str | None = None, eyebrow: str | None = None) -> None:
    """A step or sub-section heading. Sentence case for the title; the eyebrow
    is the only thing that goes ALL-CAPS, and the CSS applies that itself."""
    parts = ['<div class="cmt-section">']
    if eyebrow:
        parts.append(f'<div class="cmt-eyebrow">{_esc(eyebrow)}</div>')
    parts.append(f'<div class="cmt-section__title">{_esc(title)}</div>')
    if caption:
        parts.append(f'<div class="cmt-section__caption">{_esc(caption)}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def banner(kind: str, title: str, body: str | None = None, *,
           remedy: str | None = None, where: str | None = None) -> None:
    """A status banner. ``kind`` is one of success/info/warning/danger/neutral.

    Used in place of ``st.success``/``st.error``/``st.warning``, whose default
    icons are emoji.
    """
    parts = [f'<div class="cmt-banner cmt-banner--{kind}">', '<div class="cmt-banner__title">',
             _esc(title)]
    if where:
        parts.append(f'<span class="cmt-banner__where">{_esc(where)}</span>')
    parts.append("</div>")
    if body:
        parts.append(f'<div class="cmt-banner__body">{_esc(body)}</div>')
    if remedy:
        parts.append(f'<div class="cmt-banner__remedy"><strong>Fix:</strong> {_esc(remedy)}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def card(title: str, *, pill: str = "", meta: str | None = None,
         note: str | None = None) -> None:
    """A bordered white surface. ``pill`` takes a fragment from :func:`env_pill`."""
    parts = [f'<div class="cmt-card"><div class="cmt-card__title"><span>{_esc(title)}</span>']
    if pill:
        parts.append(pill)
    parts.append("</div>")
    if meta:
        parts.append(f'<div class="cmt-card__meta">{_esc(meta)}</div>')
    if note:
        parts.append(f'<div class="cmt-card__note">{_esc(note)}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def figures(items: Sequence[tuple[str, object]], *, tones: Mapping[str, str] | None = None) -> None:
    """A row of key figures. ``tones`` maps a label to write/danger/muted."""
    tones = tones or {}
    cells = []
    for label, value in items:
        tone = tones.get(label)
        modifier = f" cmt-figure--{tone}" if tone else ""
        cells.append(
            f'<div class="cmt-figure{modifier}"><div class="cmt-figure__value">{_esc(value)}</div>'
            f'<div class="cmt-figure__label">{_esc(label)}</div></div>'
        )
    st.markdown(f'<div class="cmt-figures">{"".join(cells)}</div>', unsafe_allow_html=True)


def checklist(items: Iterable[str]) -> None:
    """A bulleted list using the cyan checkmark — the brand's signature marker."""
    lis = "".join(f"<li>{CHECK_SVG}<span>{_esc(i)}</span></li>" for i in items)
    st.markdown(f'<ul class="cmt-checklist">{lis}</ul>', unsafe_allow_html=True)
