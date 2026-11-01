"""Node-driven regression coverage for the shared color picker's stacking.

The picker takes its z-index from the live tool-window stack when it opens, but
the fallback eyedropper keeps it open across clicks that raise other windows,
and the tool-window counters can jump far past the value it took. This is
hardening that refs #6507, not a reproduction of that report's steps. Drives the
real colorPicker.js against a small fake DOM, without importing the browser-heavy
UI modules.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PICKER = ROOT / "static" / "js" / "colorPicker.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")

HARNESS = textwrap.dedent(
    f"""
    import assert from 'node:assert/strict';

    const makeEl = () => {{
      const listeners = new Map();
      const parts = new Map();
      return {{
        style: {{}}, dataset: {{}}, children: [], parentNode: null, value: '',
        classList: {{ add() {{}}, contains: () => false }},
        addEventListener(type, fn) {{ listeners.set(type, [...(listeners.get(type) || []), fn]); }},
        removeEventListener(type, fn) {{ listeners.set(type, (listeners.get(type) || []).filter(f => f !== fn)); }},
        async fire(type, event = {{}}) {{
          const e = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, stopImmediatePropagation() {{}}, ...event }};
          for (const fn of listeners.get(type) || []) await fn(e);
        }},
        querySelector(sel) {{ if (!parts.has(sel)) parts.set(sel, makeEl()); return parts.get(sel); }},
        querySelectorAll() {{ return []; }},
        appendChild(child) {{ child.parentNode = this; this.children.push(child); return child; }},
        removeChild(child) {{ child.parentNode = null; this.children = this.children.filter(c => c !== child); }},
        remove() {{ this.parentNode?.removeChild(this); }},
        contains(other) {{ return other === this; }},
        getBoundingClientRect: () => ({{ left: 0, top: 0, width: 240, height: 340 }}),
        getAttribute: () => null,
        setAttribute() {{}},
      }};
    }};

    const body = makeEl();
    const documentEl = makeEl();
    const modals = [];
    const frames = [];
    const observers = new Set();
    Object.assign(globalThis, {{
      document: Object.assign(documentEl, {{
        body,
        createElement: makeEl,
        querySelectorAll: () => modals,
        elementsFromPoint: () => [],
      }}),
      window: {{ addEventListener() {{}}, matchMedia: () => ({{ matches: false }}), innerWidth: 1400, innerHeight: 900 }},
      localStorage: {{ getItem: () => null, setItem() {{}} }},
      requestAnimationFrame: (fn) => frames.push(fn),
      getComputedStyle: (el) => el.style,
      HTMLInputElement: class {{ get value() {{ return this._value; }} set value(v) {{ this._value = v; }} }},
      MutationObserver: class {{
        constructor(callback) {{ this.callback = callback; }}
        observe(target, options) {{ Object.assign(this, {{ target, options }}); observers.add(this); }}
        disconnect() {{ observers.delete(this); }}
      }},
    }});

    const addWindow = (z) => {{
      const modal = Object.assign(makeEl(), {{ style: {{ zIndex: String(z) }}, parentNode: body }});
      modals.push(modal);
      return modal;
    }};
    const raise = (modal, z) => {{
      modal.style.zIndex = String(z);
      observers.forEach(o => o.callback([{{ type: 'attributes', target: modal }}]));
    }};
    const flushFrames = () => frames.splice(0).forEach(fn => fn());

    const {{ attachColorPicker }} = await import({PICKER.as_uri()!r});
    const openPicker = async () => {{
      const input = Object.assign(new HTMLInputElement(), makeEl());
      attachColorPicker(input);
      await input.fire('mousedown');
      flushFrames();
      return {{ input, popover: body.children.find(c => c.className === 'cp-popover') }};
    }};
    const pickerZ = (popover) => parseInt(popover.style.zIndex, 10);
    """
)


def _run(scenario: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=HARNESS + textwrap.dedent(scenario),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_picker_stays_above_a_window_raised_while_it_is_open():
    # A confirm dialog leaves the bring-to-front counter near 100000 with the
    # live stack still low, so the next raise jumps straight over the picker.
    _run(
        """
        const theme = addWindow(1002);
        const { input, popover } = await openPicker();
        assert.equal(pickerZ(popover), 10031);
        raise(theme, 100000);
        assert.ok(pickerZ(popover) > 100000, `picker ${pickerZ(popover)} is behind the window`);
        raise(theme, 100001);
        assert.ok(pickerZ(popover) > 100001, `picker ${pickerZ(popover)} is behind the window`);
        await input.fire('mousedown');
        assert.equal(observers.size, 0, 'closing the picker must stop following the stack');
        """
    )


def test_picker_stays_above_a_window_added_while_it_is_open():
    _run(
        """
        const { popover } = await openPicker();
        addWindow(100000);
        observers.forEach(o => o.callback([{ type: 'childList', target: body }]));
        assert.ok(pickerZ(popover) > 100000, `picker ${pickerZ(popover)} is behind the window`);
        """
    )


def test_picker_stays_above_windows_raised_during_the_fallback_eyedropper():
    # Sampling detaches the outside-click handlers on purpose, so each click can
    # bring a window forward and the picker stays open through it.
    _run(
        """
        const calendar = addWindow(1001);
        const { popover } = await openPicker();
        const sampling = popover.querySelector('.cp-eyedropper').fire('click');
        raise(calendar, 100000);
        await documentEl.fire('click', { clientX: 5, clientY: 5 });
        await sampling;
        assert.ok(pickerZ(popover) > 100000, `picker ${pickerZ(popover)} is behind the window`);
        """
    )


def test_picker_ignores_style_changes_below_the_top_level_windows():
    # Live preview and streaming chat restyle nested nodes constantly; only the
    # top-level windows can change the stack.
    _run(
        """
        const nested = Object.assign(makeEl(), { parentNode: makeEl() });
        const { popover } = await openPicker();
        addWindow(100000);
        observers.forEach(o => o.callback([{ type: 'attributes', target: nested }]));
        assert.equal(pickerZ(popover), 10031);
        raise(modals[0], 100000);
        assert.ok(pickerZ(popover) > 100000, `picker ${pickerZ(popover)} is behind the window`);
        """
    )


def test_picker_observes_every_change_that_can_move_a_top_level_window():
    # The tests above hand records to the fake, so pin what a real observer needs
    # to get them: z-index and `hidden` change on body's children, which takes
    # subtree, and a new window is a childList record on body.
    _run(
        """
        await openPicker();
        const [observer] = observers;
        assert.ok(observer, 'the open picker must follow the tool-window stack');
        assert.equal(observer.target, body);
        const { subtree, childList, attributes, attributeFilter } = observer.options;
        assert.deepEqual({ subtree, childList, attributes }, { subtree: true, childList: true, attributes: true });
        assert.deepEqual([...attributeFilter].sort(), ['class', 'style']);
        """
    )
