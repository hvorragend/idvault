/* Reporting-Visualisierung — ApexCharts-Instanzen für /berichte/ (Tab "Visualisierung").
 * Liest die serverseitig aggregierten Zahlen aus <script id="reports-chart-data">
 * und rendert acht Diagramme ins Grid. Keine Netzwerk-Requests, rein clientseitig.
 */
(function () {
  "use strict";

  var dataEl = document.getElementById("reports-chart-data");
  if (!dataEl || typeof ApexCharts === "undefined") return;

  var D;
  try { D = JSON.parse(dataEl.textContent || "{}"); } catch (e) { return; }

  // idvault-Farbpalette (konsistent zu base.html :root-Variablen)
  var COLOR_PRIMARY = "#1a3a5c";
  var COLOR_ACCENT  = "#e8650a";
  var PALETTE = ["#1a3a5c", "#e8650a", "#16a34a", "#0891b2", "#7c3aed", "#ca8a04",
                 "#db2777", "#14b8a6", "#f97316", "#6366f1", "#84cc16"];

  var BASE = {
    chart: { fontFamily: "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif",
             toolbar: { show: false }, animations: { speed: 400 } },
    noData: { text: "Keine Daten für den gewählten Zeitraum.",
              style: { color: "#6b7280", fontSize: "14px" } },
  };

  function renderIfAny(el, optionsFactory, hasData) {
    if (!el) return null;
    if (!hasData) {
      el.innerHTML = '<div class="text-center text-muted py-5">' +
                     '<i class="bi bi-inbox fs-3 d-block mb-2"></i>Keine Daten.</div>';
      return null;
    }
    var chart = new ApexCharts(el, optionsFactory());
    chart.render();
    return chart;
  }

  // ── 1. Fachbereich × Status — horizontaler gestapelter Balken ──────────
  renderIfAny(
    document.getElementById("chart-by-oe-status"),
    function () {
      var oes = D.by_oe_status.oes, series = D.by_oe_status.series;
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "bar", stacked: true, height: 420 }),
        series: series,
        colors: series.map(function (s) { return s.color || COLOR_PRIMARY; }),
        plotOptions: { bar: { horizontal: true, borderRadius: 3, barHeight: "72%" } },
        dataLabels: { enabled: false },
        xaxis: { categories: oes, title: { text: "Anzahl Anwendungen" } },
        yaxis: { labels: { style: { fontSize: "12px" }, maxWidth: 220 } },
        legend: { position: "bottom", fontSize: "12px" },
        tooltip: { y: { formatter: function (v) { return v + " Anwendung" + (v === 1 ? "" : "en"); } } },
      });
    },
    D.by_oe_status && D.by_oe_status.oes && D.by_oe_status.oes.length > 0
  );

  // ── 2. Status-Donut ────────────────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-status-donut"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 320 }),
        series: D.status_donut.values,
        labels: D.status_donut.labels,
        colors: D.status_donut.colors,
        legend: { position: "bottom", fontSize: "12px" },
        plotOptions: { pie: { donut: { size: "62%",
          labels: { show: true,
            total: { show: true, label: "Gesamt",
                     formatter: function (w) {
                       return w.globals.seriesTotals.reduce(function (a, b) { return a + b; }, 0);
                     } } } } } },
        dataLabels: { style: { fontSize: "11px" } },
      });
    },
    D.status_donut && D.status_donut.values && D.status_donut.values.length > 0
  );

  // ── 3. idv_typ-Donut ───────────────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-idv-typ"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 320 }),
        series: D.idv_typ_donut.values,
        labels: D.idv_typ_donut.labels,
        colors: PALETTE,
        legend: { position: "bottom", fontSize: "12px" },
        plotOptions: { pie: { donut: { size: "60%" } } },
      });
    },
    D.idv_typ_donut && D.idv_typ_donut.values && D.idv_typ_donut.values.length > 0
  );

  // ── 4. entwicklungsart-Donut ───────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-entwicklungsart"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 320 }),
        series: D.entwicklungsart_donut.values,
        labels: D.entwicklungsart_donut.labels,
        colors: [COLOR_PRIMARY, COLOR_ACCENT, "#16a34a", "#7c3aed", "#64748b"],
        legend: { position: "bottom", fontSize: "12px" },
        plotOptions: { pie: { donut: { size: "60%" } } },
      });
    },
    D.entwicklungsart_donut && D.entwicklungsart_donut.values && D.entwicklungsart_donut.values.length > 0
  );

  // ── 5. Zeitverlauf mit Umschalter ──────────────────────────────────────
  var verlaufEl = document.getElementById("chart-verlauf");
  var verlaufChart = null;
  var verlaufMode = "gesamt";

  function verlaufSeries(mode) {
    if (mode === "by_oe") return D.verlauf.by_oe;
    return [
      { name: "Registriert",  data: D.verlauf.gesamt.registriert },
      { name: "Freigegeben",  data: D.verlauf.gesamt.freigegeben },
    ];
  }

  function verlaufColors(mode) {
    return mode === "by_oe" ? PALETTE : [COLOR_PRIMARY, "#16a34a"];
  }

  if (verlaufEl && D.verlauf && D.verlauf.monate && D.verlauf.monate.length) {
    verlaufChart = new ApexCharts(verlaufEl, Object.assign({}, BASE, {
      chart: Object.assign({}, BASE.chart, { type: "area", height: 360,
               toolbar: { show: true, tools: { download: true, selection: false, zoom: false,
                                                zoomin: false, zoomout: false, pan: false, reset: false } } }),
      series: verlaufSeries("gesamt"),
      colors: verlaufColors("gesamt"),
      stroke: { curve: "smooth", width: 2 },
      fill: { type: "gradient", gradient: { opacityFrom: 0.45, opacityTo: 0.05 } },
      dataLabels: { enabled: false },
      markers: { size: 3, hover: { size: 5 } },
      xaxis: { categories: D.verlauf.monate,
               labels: { rotate: -45, rotateAlways: false, style: { fontSize: "11px" } } },
      yaxis: { min: 0, forceNiceScale: true,
               title: { text: "Anzahl pro Monat" },
               labels: { formatter: function (v) { return Math.round(v); } } },
      legend: { position: "bottom", fontSize: "12px" },
      tooltip: { shared: true, intersect: false },
    }));
    verlaufChart.render();

    var toggle = document.getElementById("verlauf-mode-toggle");
    if (toggle) {
      toggle.addEventListener("click", function (ev) {
        var btn = ev.target.closest("button[data-mode]");
        if (!btn || btn.classList.contains("active")) return;
        toggle.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        verlaufMode = btn.getAttribute("data-mode");
        verlaufChart.updateOptions({
          series: verlaufSeries(verlaufMode),
          colors: verlaufColors(verlaufMode),
        }, false, true);
      });
    }
  } else if (verlaufEl) {
    verlaufEl.innerHTML = '<div class="text-center text-muted py-5">' +
                          '<i class="bi bi-inbox fs-3 d-block mb-2"></i>Keine Daten.</div>';
  }

  // ── 6. Heatmap Fachbereich × Anwendungsart ─────────────────────────────
  renderIfAny(
    document.getElementById("chart-heatmap"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "heatmap", height: 360 }),
        series: D.heatmap.series,
        colors: [COLOR_PRIMARY],
        dataLabels: { enabled: true, style: { fontSize: "11px", colors: ["#fff"] },
                      formatter: function (v) { return v > 0 ? v : ""; } },
        plotOptions: { heatmap: { shadeIntensity: 0.65, radius: 2, enableShades: true,
          colorScale: { ranges: [
            { from: 0, to: 0,   color: "#f1f5f9", name: "keine" },
            { from: 1, to: 999, color: COLOR_PRIMARY, name: "≥ 1" },
          ] } } },
        xaxis: { labels: { rotate: -35, style: { fontSize: "11px" } } },
        yaxis: { labels: { style: { fontSize: "11px" } } },
        legend: { show: false },
      });
    },
    D.heatmap && D.heatmap.series && D.heatmap.series.length > 0 &&
      D.heatmap.series.some(function (s) { return s.data && s.data.length > 0; })
  );

  // ── 7. Freigabe-Funnel ─────────────────────────────────────────────────
  // ApexCharts hat keinen nativen Funnel-Typ — wir bauen ihn als horizontalen
  // Balken mit abnehmenden Werten (optisch funnel-ähnlich).
  renderIfAny(
    document.getElementById("chart-funnel"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "bar", height: 320 }),
        series: [{ name: "Anzahl", data: D.funnel.values }],
        colors: ["#94a3b8", "#60a5fa", "#22c55e"],
        plotOptions: { bar: { horizontal: true, barHeight: "72%", distributed: true, borderRadius: 4,
          dataLabels: { position: "center" } } },
        dataLabels: { enabled: true, style: { fontSize: "13px", colors: ["#fff"] },
                      formatter: function (v) { return v; } },
        xaxis: { categories: D.funnel.labels, labels: { show: false } },
        yaxis: { labels: { style: { fontSize: "12px" } } },
        legend: { show: false },
        grid: { show: false },
        tooltip: { y: { formatter: function (v) { return v + " Anwendung" + (v === 1 ? "" : "en"); } } },
      });
    },
    D.funnel && D.funnel.values && D.funnel.values.some(function (v) { return v > 0; })
  );

  // ── 8. Wesentlichkeits-Ampel ───────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-ampel"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "bar", height: 420 }),
        series: [
          { name: "Wesentlich gesamt", data: D.ampel.wesentlich },
          { name: "Davon freigegeben", data: D.ampel.freigegeben },
          { name: "Davon überfällig",  data: D.ampel.ueberfaellig },
        ],
        colors: ["#0891b2", "#16a34a", "#dc2626"],
        plotOptions: { bar: { horizontal: true, borderRadius: 3, barHeight: "76%",
                              dataLabels: { position: "top" } } },
        dataLabels: { enabled: true, offsetX: 18, style: { fontSize: "11px", colors: ["#374151"] },
                      formatter: function (v) { return v || ""; } },
        xaxis: { categories: D.ampel.oes, title: { text: "Anzahl wesentlicher Anwendungen" } },
        yaxis: { labels: { style: { fontSize: "12px" }, maxWidth: 220 } },
        legend: { position: "bottom", fontSize: "12px" },
      });
    },
    D.ampel && D.ampel.oes && D.ampel.oes.length > 0
  );
})();
