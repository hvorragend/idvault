/* ============================================================================
 * idvault — Sektions-Navigation in langen Formularen (Issue #415)
 *
 * Bindet sich an .idv-form-nav-Tabs und markiert den Tab aktiv, dessen
 * Ziel-Sektion (#href) aktuell sichtbar ist (Intersection Observer).
 *
 * Smooth-Scrolling erfolgt rein ueber CSS (scroll-behavior: smooth) bzw.
 * den nativen Anker-Sprung des Browsers.
 * ========================================================================== */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var nav = document.querySelector('.idv-form-nav');
    if (!nav) return;

    var tabs = Array.from(nav.querySelectorAll('a.idv-form-nav-tab'));
    if (!tabs.length) return;

    // Map href -> tab
    var byId = {};
    var sections = [];
    tabs.forEach(function (t) {
      var id = (t.getAttribute('href') || '').replace(/^#/, '');
      if (!id) return;
      var sec = document.getElementById(id);
      if (sec) { byId[id] = t; sections.push(sec); }
    });
    if (!sections.length) return;

    // Erste Sektion initial markieren
    setActive(tabs[0]);

    function setActive(tab) {
      tabs.forEach(function (t) {
        t.classList.remove('active');
        t.removeAttribute('aria-current');
      });
      if (tab) {
        tab.classList.add('active');
        tab.setAttribute('aria-current', 'true');
      }
    }

    if (!('IntersectionObserver' in window)) return;

    var obs = new IntersectionObserver(function (entries) {
      // Beste sichtbare Sektion bestimmen (groesste intersectionRatio)
      var best = null;
      entries.forEach(function (e) {
        if (!best || e.intersectionRatio > best.intersectionRatio) best = e;
      });
      if (best && best.isIntersecting && byId[best.target.id]) {
        setActive(byId[best.target.id]);
      }
    }, {
      // Nur den oberen Drittel des Viewports betrachten – sonst flackert
      // die Markierung waehrend des Scrollens.
      rootMargin: '-30% 0px -55% 0px',
      threshold: [0, .25, .5, .75, 1]
    });

    sections.forEach(function (s) { obs.observe(s); });
  });
})();
