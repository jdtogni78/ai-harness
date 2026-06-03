"""Fake-cursor overlay injected into every page.

Playwright's recordVideo captures DOM rendering, not the OS cursor, so a real
mouse path would be invisible in the output. We inject a small glowing div and
move it in sync with `page.mouse.move(...)`.
"""

CURSOR_CSS = """
#__narrate_cursor {
  position: fixed; top: 0; left: 0; width: 18px; height: 18px;
  background: radial-gradient(circle at 50% 50%,
    rgba(255,255,255,0.92) 0%,
    rgba(255,255,255,0.55) 55%,
    transparent 75%);
  border-radius: 50%; pointer-events: none; z-index: 2147483647;
  box-shadow:
    0 0 0 1.25px rgba(0,0,0,0.45),
    0 1px 6px 2px rgba(0,0,0,0.22);
  transition: transform 0.08s linear;
  transform: translate(-9999px, -9999px);
}
"""

# Runs on every navigation via context.add_init_script — cursor survives goto().
INIT_SCRIPT = r"""
(function () {
  function install() {
    if (!document.body) { setTimeout(install, 10); return; }
    if (document.getElementById('__narrate_cursor')) return;
    var style = document.createElement('style');
    style.textContent = __CSS__;
    document.head.appendChild(style);
    var dot = document.createElement('div');
    dot.id = '__narrate_cursor';
    document.body.appendChild(dot);
    window.__narrateMoveCursor = function (x, y) {
      var half = (dot.offsetWidth || 18) / 2;
      dot.style.transform = 'translate(' + (x - half) + 'px,' + (y - half) + 'px)';
    };
  }
  install();
})();
""".replace("__CSS__", repr(CURSOR_CSS))
