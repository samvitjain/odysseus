"""Regression for #6061: the Deep Research "Discuss" primer is a system banner.

The spin-off session's first history row is role "system" with
metadata.research_spinoff_from. It must not render as an AI bubble
(msg-ai), and it should start collapsed so the conversation leads.
"""

from pathlib import Path

from tests.helpers.stylesheets import app_css


ROOT = Path(__file__).resolve().parents[1]
RENDERER = (ROOT / "static/js/chatRenderer.js").read_text(encoding="utf-8")
STYLE = app_css()


def _spinoff_branch() -> str:
    start = RENDERER.index("if (role === 'system' && metadata?.research_spinoff_from) {")
    end = RENDERER.index("// --- Standard single-bubble message ---", start)
    return RENDERER[start:end]


def test_spinoff_context_is_not_rendered_as_ai_bubble():
    branch = _spinoff_branch()
    assert "wrap.className = 'msg msg-system research-context-msg';" in branch
    assert "msg-ai" not in branch


def test_spinoff_context_reuses_collapsible_and_starts_collapsed():
    branch = _spinoff_branch()
    assert "markdownModule.createCollapsible(String(textRaw || ''), 'Research Context', false)" in branch
    assert "'Research Context', true)" not in branch


def test_spinoff_branch_runs_before_the_generic_bubble():
    assert RENDERER.index("metadata?.research_spinoff_from") < RENDERER.index(
        "wrap.className = 'msg ' + (role === 'user' ? 'msg-user' : 'msg-ai');"
    )


def test_spinoff_context_styles_extend_existing_system_chip():
    assert ".msg.msg-system {" in STYLE
    assert ".msg.msg-system.research-context-msg .role {" in STYLE
    assert "body.hide-thinking .msg.msg-system.research-context-msg .thinking-section" in STYLE
