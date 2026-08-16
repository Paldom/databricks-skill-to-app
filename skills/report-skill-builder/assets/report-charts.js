/* Renders every chart block with Apache ECharts, from the JSON the renderer embedded.
 *
 * ECharts is what AppKit itself ships (@databricks/appkit-ui depends on echarts and
 * echarts-for-react), so the report and the Databricks App draw with the same library. It also
 * needs no framework: one script instead of React + ReactDOM + prop-types + a chart lib.
 *
 * The option objects below are plain data. Hand the same `option` to <ReactECharts option={...} />
 * inside AppKit and you get the identical chart — that is the port.
 *
 * Two settings are load-bearing rather than cosmetic:
 *   animation:false — an animated series is mid-flight when a screenshot, print or PDF is taken,
 *     so a static capture of an animated chart is a blank or half-drawn chart.
 *   axis label fontSize 15 — chart libraries default to ~12px, under the legibility floor.
 */
(function () {
  var slot = document.getElementById("chart-data");
  if (!slot || !window.echarts) return;   // fallback text stays in place

  var specs;
  try {
    specs = JSON.parse(slot.textContent);
  } catch (e) {
    return;
  }

  var css = getComputedStyle(document.documentElement);
  var v = function (name) { return css.getPropertyValue(name).trim(); };
  var INK = v("--foreground"), MUTED = v("--muted-foreground"),
      LINE = v("--border"), CARD = v("--card"),
      C1 = v("--chart-1"), C2 = v("--chart-2"), C3 = v("--chart-3");

  var AXIS_LABEL = { fontSize: 15, color: MUTED, hideOverlap: true };
  var textStyle = { fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Arial" };

  function baseOption(spec) {
    return {
      animation: false,
      textStyle: textStyle,
      grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: CARD,
        borderColor: LINE,
        borderWidth: 1,
        textStyle: { color: INK, fontSize: 14 },
        extraCssText: "border-radius:8px; box-shadow:0 1px 2px 0 rgba(0,0,0,.05);"
      },
      xAxis: {
        type: "category",
        data: spec.data.map(function (d) { return d[spec.xKey]; }),
        axisLine: { show: false },          // shadcn convention: no axis line, no tick marks
        axisTick: { show: false },
        axisLabel: AXIS_LABEL,
        boundaryGap: spec.type === "bar"
      },
      yAxis: {
        type: "value",
        min: spec.domain ? spec.domain[0] : null,
        max: spec.domain ? spec.domain[1] : null,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: AXIS_LABEL,
        splitLine: { lineStyle: { color: LINE, type: "dashed" } }   // horizontal grid only
      }
    };
  }

  function seriesFor(spec, colors) {
    return spec.series.map(function (s, i) {
      var color = colors[i % colors.length];
      var values = spec.data.map(function (d) {
        return d[s.key] === undefined ? null : d[s.key];
      });
      if (spec.type === "bar") {
        return {
          name: s.label, type: "bar", data: values,
          itemStyle: { color: color, borderRadius: [4, 4, 0, 0] },
          barMaxWidth: 64
        };
      }
      return {
        name: s.label, type: "line", data: values, smooth: true, showSymbol: false,
        lineStyle: { color: color, width: 2 },
        itemStyle: { color: color },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: color },
              { offset: 1, color: color }
            ]
          },
          opacity: 0.18
        }
      };
    });
  }

  var charts = [];
  Object.keys(specs).forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    var spec = specs[id];
    el.textContent = "";
    var chart = echarts.init(el, null, { renderer: "svg" });   // SVG prints and scales cleanly
    var option = baseOption(spec);
    option.series = seriesFor(spec, [C1, C2, C3]);
    chart.setOption(option);
    charts.push(chart);
  });

  window.addEventListener("resize", function () {
    charts.forEach(function (c) { c.resize(); });
  });
})();
