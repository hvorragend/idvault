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

  // idvscope-Farbpalette (konsistent zu base.html :root-Variablen)
  var COLOR_PRIMARY = "#1a3a5c";
  var COLOR_ACCENT  = "#e8650a";
  var PALETTE = ["#1a3a5c", "#e8650a", "#16a34a", "#0891b2", "#7c3aed", "#ca8a04",
                 "#db2777", "#14b8a6", "#f97316", "#6366f1", "#84cc16"];

  // Einheitliche Typografie — klein, damit die Zahlen die Diagramme nicht erschlagen.
  var LBL   = "10px";
  var LBL_S = "9px";
  var LEGEND = { position: "bottom", fontSize: "11px", itemMargin: { horizontal: 6, vertical: 2 },
                 markers: { width: 10, height: 10, radius: 2 } };
  var AXIS  = { labels: { style: { fontSize: LBL, colors: "#475569" } } };

  var BASE = {
    chart: { fontFamily: "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif",
             toolbar: { show: false }, animations: { speed: 400 },
             dropShadow: { enabled: false } },
    dataLabels: { style: { fontSize: LBL, fontWeight: 500 },
                  dropShadow: { enabled: false } },
    grid: { borderColor: "#eef2f7", strokeDashArray: 3,
            padding: { left: 10, right: 15, top: 0, bottom: 0 } },
    noData: { text: "Keine Daten für den gewählten Zeitraum.",
              style: { color: "#6b7280", fontSize: "13px" } },
    tooltip: { style: { fontSize: "12px" } },
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
      // Dynamische Höhe: pro OE ~32 px, mindestens 360, höchstens 900.
      var h = Math.min(900, Math.max(360, 90 + oes.length * 32));
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "bar", stacked: true, height: h }),
        series: series,
        colors: series.map(function (s) { return s.color || COLOR_PRIMARY; }),
        plotOptions: { bar: { horizontal: true, borderRadius: 3, barHeight: "68%" } },
        dataLabels: { enabled: false },
        stroke: { show: true, width: 1, colors: ["#fff"] },
        xaxis: Object.assign({}, AXIS, { categories: oes,
                 title: { text: "Anzahl Anwendungen", style: { fontSize: LBL, fontWeight: 500 } } }),
        yaxis: { labels: { style: { fontSize: LBL, colors: "#1f2937" }, maxWidth: 220 } },
        legend: LEGEND,
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
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 340 }),
        series: D.status_donut.values,
        labels: D.status_donut.labels,
        colors: D.status_donut.colors,
        legend: LEGEND,
        stroke: { width: 2, colors: ["#fff"] },
        plotOptions: { pie: { donut: { size: "68%",
          labels: { show: true,
            name:  { fontSize: "11px", color: "#64748b", offsetY: -4 },
            value: { fontSize: "20px", fontWeight: 600, color: "#1a3a5c", offsetY: 4 },
            total: { show: true, label: "Gesamt", fontSize: "11px",
                     color: "#64748b",
                     formatter: function (w) {
                       return w.globals.seriesTotals.reduce(function (a, b) { return a + b; }, 0);
                     } } } } } },
        dataLabels: { enabled: true, style: { fontSize: LBL_S, fontWeight: 500, colors: ["#fff"] },
                      dropShadow: { enabled: false },
                      formatter: function (val) { return Math.round(val) + "%"; } },
      });
    },
    D.status_donut && D.status_donut.values && D.status_donut.values.length > 0
  );

  // ── 3. idv_typ-Donut ───────────────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-idv-typ"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 340 }),
        series: D.idv_typ_donut.values,
        labels: D.idv_typ_donut.labels,
        colors: PALETTE,
        legend: LEGEND,
        stroke: { width: 2, colors: ["#fff"] },
        plotOptions: { pie: { donut: { size: "66%",
          labels: { show: true,
            name:  { fontSize: "11px", color: "#64748b" },
            value: { fontSize: "20px", fontWeight: 600, color: "#1a3a5c" },
            total: { show: true, label: "Typen", fontSize: "11px", color: "#64748b" } } } } },
        dataLabels: { enabled: true, style: { fontSize: LBL_S, fontWeight: 500, colors: ["#fff"] },
                      dropShadow: { enabled: false },
                      formatter: function (val) { return Math.round(val) + "%"; } },
      });
    },
    D.idv_typ_donut && D.idv_typ_donut.values && D.idv_typ_donut.values.length > 0
  );

  // ── 4. entwicklungsart-Donut ───────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-entwicklungsart"),
    function () {
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "donut", height: 340 }),
        series: D.entwicklungsart_donut.values,
        labels: D.entwicklungsart_donut.labels,
        colors: [COLOR_PRIMARY, COLOR_ACCENT, "#16a34a", "#7c3aed", "#64748b"],
        legend: LEGEND,
        stroke: { width: 2, colors: ["#fff"] },
        plotOptions: { pie: { donut: { size: "66%",
          labels: { show: true,
            name:  { fontSize: "11px", color: "#64748b" },
            value: { fontSize: "20px", fontWeight: 600, color: "#1a3a5c" },
            total: { show: true, label: "Kategorien", fontSize: "11px", color: "#64748b" } } } } },
        dataLabels: { enabled: true, style: { fontSize: LBL_S, fontWeight: 500, colors: ["#fff"] },
                      dropShadow: { enabled: false },
                      formatter: function (val) { return Math.round(val) + "%"; } },
      });
    },
    D.entwicklungsart_donut && D.entwicklungsart_donut.values && D.entwicklungsart_donut.values.length > 0
  );

  // ── 5. Zeitverlauf mit Umschalter ──────────────────────────────────────
  var verlaufEl = document.getElementById("chart-verlauf");
  var verlaufChart = null;

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
      chart: Object.assign({}, BASE.chart, { type: "area", height: 380,
               toolbar: { show: true, tools: { download: true, selection: false, zoom: false,
                                                zoomin: false, zoomout: false, pan: false, reset: false } } }),
      series: verlaufSeries("gesamt"),
      colors: verlaufColors("gesamt"),
      stroke: { curve: "smooth", width: 2 },
      fill: { type: "gradient", gradient: { opacityFrom: 0.40, opacityTo: 0.02 } },
      dataLabels: { enabled: false },
      markers: { size: 3, strokeWidth: 0, hover: { size: 5 } },
      xaxis: Object.assign({}, AXIS, { categories: D.verlauf.monate,
               labels: { rotate: -45, rotateAlways: false, style: { fontSize: LBL, colors: "#475569" } },
               axisTicks: { show: false } }),
      yaxis: { min: 0, forceNiceScale: true,
               labels: { style: { fontSize: LBL, colors: "#475569" },
                         formatter: function (v) { return Math.round(v); } } },
      legend: LEGEND,
      tooltip: { shared: true, intersect: false, style: { fontSize: "12px" } },
    }));
    verlaufChart.render();

    var toggle = document.getElementById("verlauf-mode-toggle");
    if (toggle) {
      toggle.addEventListener("click", function (ev) {
        var btn = ev.target.closest("button[data-mode]");
        if (!btn || btn.classList.contains("active")) return;
        toggle.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        var mode = btn.getAttribute("data-mode");
        verlaufChart.updateOptions({
          series: verlaufSeries(mode),
          colors: verlaufColors(mode),
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
      var rows = D.heatmap.series.length;
      var h = Math.min(640, Math.max(340, 80 + rows * 32));
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "heatmap", height: h }),
        series: D.heatmap.series,
        colors: [COLOR_PRIMARY],
        dataLabels: { enabled: true,
                      style: { fontSize: LBL_S, fontWeight: 500, colors: ["#fff"] },
                      dropShadow: { enabled: false },
                      formatter: function (v) { return v > 0 ? v : ""; } },
        stroke: { width: 1, colors: ["#fff"] },
        plotOptions: { heatmap: { shadeIntensity: 0.7, radius: 2, enableShades: true,
          colorScale: { ranges: [
            { from: 0, to: 0,   color: "#f1f5f9", name: "keine" },
            { from: 1, to: 999, color: COLOR_PRIMARY, name: "≥ 1" },
          ] } } },
        xaxis: { labels: { rotate: -35, rotateAlways: false,
                           style: { fontSize: LBL, colors: "#475569" } } },
        yaxis: { labels: { style: { fontSize: LBL, colors: "#1f2937" } } },
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
        chart: Object.assign({}, BASE.chart, { type: "bar", height: 340 }),
        series: [{ name: "Anzahl", data: D.funnel.values }],
        colors: ["#94a3b8", "#60a5fa", "#22c55e"],
        plotOptions: { bar: { horizontal: true, barHeight: "74%", distributed: true, borderRadius: 4,
          dataLabels: { position: "center" } } },
        dataLabels: { enabled: true, style: { fontSize: "11px", fontWeight: 600, colors: ["#fff"] },
                      dropShadow: { enabled: false },
                      formatter: function (v) { return v; } },
        xaxis: { categories: D.funnel.labels, labels: { show: false }, axisTicks: { show: false },
                 axisBorder: { show: false } },
        yaxis: { labels: { style: { fontSize: LBL, colors: "#1f2937", fontWeight: 500 } } },
        legend: { show: false },
        grid: { show: false, padding: { left: 10, right: 20 } },
        tooltip: { y: { formatter: function (v) { return v + " Anwendung" + (v === 1 ? "" : "en"); } } },
      });
    },
    D.funnel && D.funnel.values && D.funnel.values.some(function (v) { return v > 0; })
  );

  // ── 8. Wesentlichkeits-Ampel ───────────────────────────────────────────
  renderIfAny(
    document.getElementById("chart-ampel"),
    function () {
      var oes = D.ampel.oes;
      var h = Math.min(720, Math.max(360, 90 + oes.length * 34));
      return Object.assign({}, BASE, {
        chart: Object.assign({}, BASE.chart, { type: "bar", height: h }),
        series: [
          { name: "Wesentlich gesamt", data: D.ampel.wesentlich },
          { name: "Davon freigegeben", data: D.ampel.freigegeben },
          { name: "Davon überfällig",  data: D.ampel.ueberfaellig },
        ],
        colors: ["#0891b2", "#16a34a", "#dc2626"],
        plotOptions: { bar: { horizontal: true, borderRadius: 3, barHeight: "78%",
                              dataLabels: { position: "top" } } },
        dataLabels: { enabled: true, offsetX: 14, textAnchor: "start",
                      style: { fontSize: LBL, fontWeight: 500, colors: ["#374151"] },
                      dropShadow: { enabled: false },
                      formatter: function (v) { return v || ""; } },
        stroke: { show: true, width: 1, colors: ["transparent"] },
        xaxis: Object.assign({}, AXIS, { categories: oes,
                 title: { text: "Anzahl wesentlicher Anwendungen",
                          style: { fontSize: LBL, fontWeight: 500 } } }),
        yaxis: { labels: { style: { fontSize: LBL, colors: "#1f2937" }, maxWidth: 220 } },
        legend: LEGEND,
      });
    },
    D.ampel && D.ampel.oes && D.ampel.oes.length > 0
  );
})();
