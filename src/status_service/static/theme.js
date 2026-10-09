/* Theme toggle, behaving exactly like the one in yourbot.gg's footer:
   it cycles light, dark, then auto (follow the system), remembers the
   choice in localStorage under the same key, and announces a `themechange`
   event. The first paint is handled by the inline bootstrap in base.html;
   sky.js re-reads its star colours when data-theme changes. */
(function () {
  'use strict';

  var THEME_BG = { dark: '#07080d', light: '#eceef5' };

  function systemTheme() {
    return (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  }
  function stored() {
    try {
      var v = localStorage.getItem('mmo_theme');
      return (v === 'dark' || v === 'light') ? v : null;
    } catch (e) { return null; }
  }

  function paint() {
    var resolved = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
    var choice = stored();
    var label = choice ? resolved : 'system';
    document.querySelectorAll('[data-theme-toggle] .mmo-theme-icon').forEach(function (el) {
      el.textContent = label === 'light' ? '☀️' : (label === 'dark' ? '🌙' : '🖥️');
    });
    document.querySelectorAll('[data-theme-toggle]').forEach(function (b) {
      b.setAttribute('aria-pressed', choice ? 'true' : 'false');
      b.title = 'Theme: ' + label + ' (click to cycle)';
    });
    // Keep the browser chrome the colour of the page canvas.
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) {
      m.setAttribute('content', THEME_BG[resolved]);
    });
  }

  function setTheme(mode) {
    if (mode === 'system') {
      try { localStorage.removeItem('mmo_theme'); } catch (e) {}
      document.documentElement.dataset.theme = systemTheme();
    } else {
      try { localStorage.setItem('mmo_theme', mode); } catch (e) {}
      document.documentElement.dataset.theme = mode;
    }
    paint();
    document.dispatchEvent(new CustomEvent('themechange', {
      detail: { theme: document.documentElement.dataset.theme, mode: stored() || 'system' },
    }));
  }

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-theme-toggle]');
    if (!b) return;
    var cur = stored();
    setTheme(cur === 'light' ? 'dark' : (cur === 'dark' ? 'system' : 'light'));
  });

  // Follow the system while no explicit choice is stored.
  if (window.matchMedia) {
    try {
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
        if (stored()) return;
        document.documentElement.dataset.theme = e.matches ? 'light' : 'dark';
        paint();
      });
    } catch (e) {}
  }

  paint();
})();
