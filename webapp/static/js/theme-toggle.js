/* ============================================================================
 * idvault — Theme-Toggle (Light / Dark) (Issue #416)
 *
 * Setzt data-bs-theme auf <html> nach folgender Reihenfolge:
 *   1. localStorage["idv:theme"] (vom Nutzer gesetzt)
 *   2. prefers-color-scheme: dark  → "dark"
 *   3. Default                     → "light"
 *
 * Der Toggle-Button hat data-action="toggleTheme" (genutzt von der
 * existierenden data-action-Delegation in base.html).
 *
 * Hinweis: Die App startet absichtlich weiterhin standardmaessig im
 * Light-Mode; Dark-Mode ist eine Opt-in-Vorbereitung. Die zugehoerigen
 * CSS-Tokens (Sektion 42 in idvault.css) sind so gewaehlt, dass die
 * Bestandsoberflaeche unveraendert bleibt.
 * ========================================================================== */
(function () {
  'use strict';

  var KEY = 'idv:theme';

  function getStored() {
    try { return localStorage.getItem(KEY); } catch (_) { return null; }
  }
  function setStored(v) {
    try { localStorage.setItem(KEY, v); } catch (_) { /* ignore */ }
  }

  function effectiveTheme() {
    var s = getStored();
    if (s === 'dark' || s === 'light') return s;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark';
    }
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-bs-theme', theme);
    var btn = document.getElementById('theme-toggle-btn');
    if (btn) {
      var icon = btn.querySelector('i');
      if (icon) {
        icon.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon-stars';
      }
      btn.setAttribute('aria-label',
        theme === 'dark' ? 'Helles Theme aktivieren' : 'Dunkles Theme aktivieren');
      btn.title = btn.getAttribute('aria-label');
    }
  }

  // Sofort beim Laden anwenden (vor DOMContentLoaded), damit kein FOUC.
  applyTheme(effectiveTheme());

  // Toggle-Funktion via data-action="toggleTheme"
  window.toggleTheme = function () {
    var current = document.documentElement.getAttribute('data-bs-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    setStored(next);
    applyTheme(next);
  };

  // System-Theme-Wechsel respektieren, solange Nutzer nicht explizit gewaehlt hat.
  if (window.matchMedia) {
    try {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
        if (!getStored()) applyTheme(e.matches ? 'dark' : 'light');
      });
    } catch (_) { /* aeltere Safari-Versionen */ }
  }

  // Nach DOM ready einmal nachsynchronisieren (Icon sitzt erst dann im DOM).
  document.addEventListener('DOMContentLoaded', function () {
    applyTheme(effectiveTheme());
  });
})();
