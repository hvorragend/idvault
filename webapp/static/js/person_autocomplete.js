/* ----------------------------------------------------------------------------
 * Person-Autocomplete
 * Live-Suche nach Mitarbeitern (User-ID, Nach-/Vorname, E-Mail).
 *
 * Markup:
 *   <div class="person-ac" data-person-ac>
 *     <input type="hidden" name="fachverantwortlicher_id"
 *            value="{{ idv.fachverantwortlicher_id or '' }}">
 *     <input type="text" class="form-control person-ac-input"
 *            data-initial-label="{{ initial_person_label or '' }}"
 *            placeholder="Name oder User-ID …" autocomplete="off">
 *     <div class="person-ac-menu dropdown-menu w-100"></div>
 *   </div>
 *
 * Attribute am Container:
 *   data-required           leerer hidden-Wert blockt das Submit
 *   data-allow-empty        kein Eintrag = NULL erlaubt (Default)
 *   data-on-pick="<funcName>"  optionaler globaler Callback nach Auswahl
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var SEARCH_URL = '/admin/api/persons/search';
  var DEBOUNCE_MS = 150;

  function escapeHTML(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function init(root) {
    if (root.dataset.acInit === '1') return;
    root.dataset.acInit = '1';

    var input  = root.querySelector('.person-ac-input');
    var hidden = root.querySelector('input[type="hidden"]');
    var menu   = root.querySelector('.person-ac-menu');
    if (!input || !hidden || !menu) return;

    if (!input.value && input.dataset.initialLabel) {
      input.value = input.dataset.initialLabel;
    }

    var items = [];
    var idx = -1;
    var lastQuery = null;
    var timer = null;

    function close() {
      menu.classList.remove('show');
      idx = -1;
    }

    function render() {
      if (!items.length) { close(); return; }
      menu.innerHTML = items.map(function (it, i) {
        var meta = it.email ? '<span class="text-muted ms-2 small">' + escapeHTML(it.email) + '</span>' : '';
        var inactive = it.aktiv ? '' : ' <span class="badge bg-secondary ms-1">inaktiv</span>';
        return '<button type="button" class="dropdown-item' + (i === idx ? ' active' : '') +
               '" data-id="' + escapeHTML(it.id) +
               '" data-label="' + escapeHTML(it.label) + '">' +
               escapeHTML(it.label) + meta + inactive + '</button>';
      }).join('');
      menu.classList.add('show');
    }

    function pick(it) {
      hidden.value = it.id;
      input.value = it.label;
      input.classList.remove('is-invalid');
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
      var cb = root.dataset.onPick;
      if (cb && typeof window[cb] === 'function') {
        try { window[cb](it, root); } catch (e) { /* ignore */ }
      }
      close();
    }

    function search(q) {
      if (q === lastQuery) return;
      lastQuery = q;
      clearTimeout(timer);
      if (!q) { items = []; close(); return; }
      timer = setTimeout(function () {
        fetch(SEARCH_URL + '?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (data) {
            items = Array.isArray(data) ? data : [];
            idx = -1;
            render();
          })
          .catch(function () { /* ignore network errors */ });
      }, DEBOUNCE_MS);
    }

    input.addEventListener('input', function () {
      hidden.value = '';
      search(input.value.trim());
    });

    input.addEventListener('focus', function () {
      if (input.value.trim()) search(input.value.trim());
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        if (!items.length) return;
        idx = Math.min(idx + 1, items.length - 1);
        render();
        e.preventDefault();
      } else if (e.key === 'ArrowUp') {
        if (!items.length) return;
        idx = Math.max(idx - 1, 0);
        render();
        e.preventDefault();
      } else if (e.key === 'Enter') {
        if (idx >= 0 && items[idx]) {
          pick(items[idx]);
          e.preventDefault();
        } else if (items.length === 1) {
          pick(items[0]);
          e.preventDefault();
        }
      } else if (e.key === 'Escape') {
        close();
      }
    });

    menu.addEventListener('mousedown', function (e) {
      var btn = e.target.closest('button[data-id]');
      if (!btn) return;
      e.preventDefault();
      var found = items.find(function (it) { return String(it.id) === btn.dataset.id; });
      if (found) pick(found);
    });

    input.addEventListener('blur', function () {
      // kurzer Delay, damit mousedown im Menu noch greifen kann
      setTimeout(close, 150);
      // Wenn Text manuell geleert wurde, hidden synchron leeren
      if (!input.value.trim()) hidden.value = '';
    });

    // Submit-Validierung: required prüft, ob hidden gefüllt ist
    var form = root.closest('form');
    if (form && root.hasAttribute('data-required')) {
      form.addEventListener('submit', function (e) {
        if (!hidden.value) {
          e.preventDefault();
          input.classList.add('is-invalid');
          input.focus();
        }
      });
    }
  }

  function initAll(scope) {
    (scope || document).querySelectorAll('[data-person-ac]').forEach(init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { initAll(); });
  } else {
    initAll();
  }

  // Für dynamisch nachgeladene Bereiche (z. B. nach Quick-Add-Modal)
  window.PersonAutocomplete = { init: init, initAll: initAll };
})();
