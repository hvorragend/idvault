/* ============================================================================
 * idvscope — clientseitige Utility-Funktionen (Issue #416)
 *
 * Stellt einen `window.idv`-Namespace bereit:
 *   idv.toast(message, kind, opts)   – nicht-blockierende Rueckmeldung
 *   idv.escapeHTML(str)              – sichere HTML-Ausgabe
 *   idv.debounce(fn, ms)             – Standard-Debouncer
 *
 * Verwertet von:
 *   - base.html (Flash-Messages success/info werden als Toast gerendert)
 *   - kuenftig: form-eigenentwicklung.js, list-filters.js u. a.
 * ========================================================================== */
(function () {
  'use strict';

  var idv = window.idv = window.idv || {};

  // ── escapeHTML ─────────────────────────────────────────────────────────
  idv.escapeHTML = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  };

  // ── debounce ───────────────────────────────────────────────────────────
  idv.debounce = function (fn, ms) {
    var t = null;
    return function () {
      var ctx = this, args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms || 180);
    };
  };

  // ── Toasts ─────────────────────────────────────────────────────────────
  // Bootstrap-5-Toasts in einem fixierten Container oben rechts.
  // kind: 'success' (default) | 'info' | 'warning' | 'danger'
  // opts: { delay?: number = 4000, persistent?: bool = false }
  function ensureContainer() {
    var c = document.getElementById('idv-toast-container');
    if (c) return c;
    c = document.createElement('div');
    c.id = 'idv-toast-container';
    c.className = 'toast-container position-fixed top-0 end-0 p-3';
    c.style.zIndex = 'var(--toast-z, 1080)';
    c.setAttribute('aria-live', 'polite');
    c.setAttribute('aria-atomic', 'true');
    document.body.appendChild(c);
    return c;
  }

  var iconByKind = {
    success: 'bi-check-circle-fill',
    info:    'bi-info-circle-fill',
    warning: 'bi-exclamation-triangle-fill',
    danger:  'bi-x-octagon-fill'
  };

  idv.toast = function (message, kind, opts) {
    kind = kind || 'success';
    opts = opts || {};
    var icon = iconByKind[kind] || iconByKind.info;
    var container = ensureContainer();
    var el = document.createElement('div');
    el.className = 'toast idv-toast idv-toast-' + kind;
    el.setAttribute('role', kind === 'danger' ? 'alert' : 'status');
    el.setAttribute('aria-atomic', 'true');
    el.innerHTML =
      '<div class="d-flex">' +
      '  <div class="toast-body d-flex align-items-center gap-2">' +
      '    <i class="bi ' + icon + '" aria-hidden="true"></i>' +
      '    <span>' + idv.escapeHTML(message) + '</span>' +
      '  </div>' +
      '  <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast" aria-label="Schließen"></button>' +
      '</div>';
    container.appendChild(el);

    // Bootstrap-Toast initialisieren, wenn vorhanden – sonst minimal-fallback.
    var Toast = window.bootstrap && window.bootstrap.Toast;
    if (Toast) {
      var t = new Toast(el, {
        autohide: !opts.persistent,
        delay:    opts.delay || 4000
      });
      el.addEventListener('hidden.bs.toast', function () { el.remove(); });
      t.show();
    } else {
      // Fallback ohne Bootstrap-JS
      el.classList.add('show');
      if (!opts.persistent) setTimeout(function () { el.remove(); }, opts.delay || 4000);
    }
    return el;
  };

  // ── Flash-Pickup ───────────────────────────────────────────────────────
  // Server rendert in base.html versteckte <template data-toast="kind">…</template>
  // -Bloecke; wir zeigen sie nach Pageload als echte Toasts an.
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('template[data-toast]').forEach(function (tpl) {
      var kind = tpl.getAttribute('data-toast') || 'info';
      var msg  = (tpl.content && tpl.content.textContent || '').trim();
      if (msg) idv.toast(msg, kind);
      tpl.remove();
    });
  });
})();
