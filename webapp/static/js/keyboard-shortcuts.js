/* ============================================================================
 * IDVScope — keyboard shortcuts (Issue #413)
 *
 * Erweitert die in base.html bereits vorhandenen Shortcuts (Ctrl+K, N, E,
 * ?, Esc, Ctrl+S) um Power-User-Patterns:
 *
 *   /         Filter-/Suchfeld der aktuellen Seite fokussieren
 *   j / k     naechste / vorherige Zeile in einer .table-idv markieren
 *   o / ↵     markierte Zeile oeffnen (erster <a> in der Zeile)
 *   x         markierte Zeile in Bulk-Auswahl togglen (falls Checkbox vorhanden)
 *   g d       → Dashboard
 *   g e       → Eigenentwicklungen
 *   g f       → Funde
 *   g r       → Pruefungen
 *   g m       → Massnahmen
 *
 * Routen werden ueber data-shortcut-goto="<key>" auf den Sidebar-Links
 * deklariert, damit URL-Logik beim Server bleibt.
 *
 * Das Modul registriert seine Eintraege in window._extraShortcuts (das
 * bestehende Help-Modal in base.html liest die Variable und ergaenzt
 * die Uebersicht automatisch).
 * ========================================================================== */
(function () {
  'use strict';

  // ── Helpers ─────────────────────────────────────────────────────────────
  function inInput() {
    var el = document.activeElement;
    if (!el) return false;
    var t = el.tagName;
    return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' ||
           el.getAttribute('contenteditable') === 'true';
  }

  function visibleRows() {
    var rows = document.querySelectorAll('.table-idv tbody tr');
    return Array.from(rows).filter(function (r) {
      return r.offsetParent !== null && !r.classList.contains('idv-empty-row');
    });
  }

  function activeRow() {
    return document.querySelector('.table-idv tbody tr.idv-row-active');
  }

  function setActiveRow(row) {
    var prev = activeRow();
    if (prev) prev.classList.remove('idv-row-active');
    if (row) {
      row.classList.add('idv-row-active');
      row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  function moveRow(dir) {
    var rows = visibleRows();
    if (!rows.length) return;
    var cur = activeRow();
    var idx = cur ? rows.indexOf(cur) : -1;
    var next = Math.max(0, Math.min(rows.length - 1, idx + dir));
    setActiveRow(rows[next]);
  }

  function openActiveRow() {
    var row = activeRow();
    if (!row) return false;
    var link = row.querySelector('a[href]');
    if (link) { window.location.href = link.href; return true; }
    return false;
  }

  function toggleActiveRowCheckbox() {
    var row = activeRow();
    if (!row) return false;
    var cb = row.querySelector('input[type="checkbox"]');
    if (cb) {
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    return false;
  }

  function focusSearchField() {
    // Bevorzugt explizit als Such-/Filter-Feld markierte Inputs
    var sel = [
      'input[type="search"]',
      'input[name="q"]',
      'input[name="suche"]',
      'input[name="search"]',
      'input[data-shortcut-search]',
      'input.idv-filter-input'
    ].join(',');
    var el = document.querySelector(sel);
    if (el && el.offsetParent !== null) { el.focus(); el.select && el.select(); return true; }
    return false;
  }

  function gotoByKey(key) {
    var link = document.querySelector('a[data-shortcut-goto="' + key + '"]');
    if (link) { window.location.href = link.href; return true; }
    return false;
  }

  // ── Help-Eintraege registrieren (#419: gruppiert) ───────────────────────
  // Das Help-Modal in base.html liest window._shortcutGroups und rendert
  // ein zweispaltiges Layout. Wir tragen unsere Bindings in die passenden
  // Gruppen ein, anstatt in eine flache Liste.
  window._shortcutGroups = window._shortcutGroups || {};
  function ensureGroup(key, title) {
    if (!window._shortcutGroups[key]) {
      window._shortcutGroups[key] = { title: title, items: [] };
    }
    return window._shortcutGroups[key];
  }
  ensureGroup('navigation', 'Navigation').items.push(
    ['/',   'Suche/Filter fokussieren'],
    ['g d', 'Zum Dashboard'],
    ['g e', 'Zu den Eigenentwicklungen'],
    ['g f', 'Zu den Funden'],
    ['g r', 'Zu den Pruefungen'],
    ['g m', 'Zu den Massnahmen']
  );
  ensureGroup('lists', 'Listen').items.push(
    ['j / k', 'Naechste / vorherige Tabellenzeile'],
    ['o',     'Markierte Zeile oeffnen'],
    ['Enter', 'Markierte Zeile oeffnen'],
    ['x',     'Bulk-Checkbox der Zeile togglen']
  );

  // ── Key-Handling ─────────────────────────────────────────────────────────
  // g-Prefix-Buffer: nach Druck von "g" warten wir bis zu 1.2s auf den
  // zweiten Tastendruck ("d", "e", "f", "r", "m").
  var gBuffer = false;
  var gTimer = null;

  function clearG() {
    gBuffer = false;
    if (gTimer) { clearTimeout(gTimer); gTimer = null; }
  }

  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { clearG(); return; }
    if (inInput()) { clearG(); return; }

    // Zweiter Schritt einer g-Sequence
    if (gBuffer) {
      var handled = false;
      switch (e.key) {
        case 'd': handled = gotoByKey('d'); break;
        case 'e': handled = gotoByKey('e'); break;
        case 'f': handled = gotoByKey('f'); break;
        case 'r': handled = gotoByKey('r'); break;
        case 'm': handled = gotoByKey('m'); break;
      }
      clearG();
      if (handled) e.preventDefault();
      return;
    }

    switch (e.key) {
      case 'g':
        gBuffer = true;
        gTimer = setTimeout(clearG, 1200);
        e.preventDefault();
        break;
      case '/':
        if (focusSearchField()) e.preventDefault();
        break;
      case 'j':
        moveRow(+1);
        e.preventDefault();
        break;
      case 'k':
        moveRow(-1);
        e.preventDefault();
        break;
      case 'o':
        if (openActiveRow()) e.preventDefault();
        break;
      case 'Enter':
        // Enter darf normales Submit nicht stoeren – nur greifen, wenn
        // wir explizit eine markierte Zeile haben.
        if (activeRow() && openActiveRow()) e.preventDefault();
        break;
      case 'x':
        if (toggleActiveRowCheckbox()) e.preventDefault();
        break;
    }
  });

  // ── Initiale Zeile markieren, wenn der Nutzer mit j/k anfaengt ──────────
  // (kein Auto-Highlight, damit das Layout ruhig bleibt.)
})();
