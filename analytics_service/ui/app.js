"use strict";
const $ = (id) => document.getElementById(id);
let token = "";
let charts = [];
let metrics = [];
let generation = 0;
const title = (name) => name.replaceAll("_", " ");
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {Authorization: `Bearer ${token}`, ...(body ? {"Content-Type": "application/json"} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || "Request failed.");
  return result;
}
function clearResults() {
  charts.forEach((chart) => chart.destroy());
  charts = [];
  $("results").hidden = true;
  $("empty").hidden = false;
}
$("access-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  generation += 1;
  clearResults();
  token = $("token").value;
  $("message").textContent = "";
  try {
    const response = await api("/metrics");
    metrics = response.questions;
    $("metric").replaceChildren(...metrics.map((metric) => {
      const option = element("option", title(metric.id)); option.value = metric.id; return option;
    }));
    $("question").value = metrics[0].question;
    for (const id of ["metric", "question", "run"]) $(id).disabled = false;
    $("scope").textContent = response.scope.join(" / ");
    $("status").textContent = response.mode === "offline_fixture" ? "Offline fixture" : "Authenticated";
    $("empty-status").textContent = "Ready";
    $("token").value = "";
    $("disconnect").hidden = false;
  } catch (error) {
    disconnect();
    $("message").textContent = error.message;
  }
});
function disconnect() {
  generation += 1;
  token = "";
  clearResults();
  for (const id of ["metric", "question", "run"]) $(id).disabled = true;
  $("status").textContent = "Not connected";
  $("scope").textContent = "";
  $("empty-status").textContent = "Authentication required";
  $("disconnect").hidden = true;
  $("token").value = "";
}
$("disconnect").addEventListener("click", disconnect);
$("metric").addEventListener("change", () => {
  $("question").value = metrics.find((metric) => metric.id === $("metric").value).question;
});
$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const thisGeneration = generation;
  clearResults();
  $("run").disabled = true;
  $("run").textContent = "Running...";
  $("message").textContent = "";
  $("empty-status").textContent = "Query in progress";
  try {
    const result = await api("/query", {question: $("question").value});
    if (thisGeneration !== generation) return;
    render(result);
  } catch (error) {
    if (thisGeneration !== generation) return;
    $("message").textContent = error.message;
    $("empty-status").textContent = "Query not completed";
  } finally {
    $("run").textContent = "Run query";
    $("run").disabled = !token;
  }
});
function render(result) {
  $("empty").hidden = true;
  $("results").hidden = false;
  $("metric-title").textContent = title(result.metric);
  $("status").textContent = result.mode === "offline_fixture" ? "Offline fixture" : "BigQuery result";
  $("freshness").textContent = `As of ${result.data_as_of.slice(0, 10)} · ${result.timezone}`;
  $("warnings").replaceChildren(...result.warnings.map((warning) => element("div", warning)));
  $("insights").replaceChildren(...result.dashboard.insights.map((insight) => element("li", insight)));
  $("kpis").replaceChildren();
  $("charts").replaceChildren();
  const colors = ["#168477", "#a71d32", "#d3a53b", "#667c89"];
  for (const spec of result.dashboard.charts) {
    if (spec.kind === "kpi") {
      const node = element("div", undefined, "kpi");
      node.append(element("div", title(spec.measure), "kpi-label"),
        element("div", spec.value ?? "Not available", "kpi-value"), element("div", spec.unit, "kpi-unit"));
      $("kpis").append(node);
      continue;
    }
    const pane = element("div", undefined, "chart");
    const wrap = element("div", undefined, "canvas-wrap");
    const canvas = document.createElement("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${title(spec.y)} by ${title(spec.x)}; values are in the query results table`);
    wrap.append(canvas);
    pane.append(element("h3", `${title(spec.y)} / ${spec.unit}`), wrap);
    $("charts").append(pane);
    const labels = [...new Set(result.rows.map((row) => row[spec.x]))].sort();
    const groups = spec.series ? [...new Set(result.rows.map((row) => row[spec.series]))] : [null];
    const datasets = groups.map((group, index) => ({
      label: group ?? title(spec.y), borderColor: colors[index % colors.length],
      backgroundColor: colors[index % colors.length], borderWidth: 2, pointRadius: 3,
      spanGaps: false, data: labels.map((label) => {
        const row = result.rows.find((value) => value[spec.x] === label && (!spec.series || value[spec.series] === group));
        return row?.[spec.y] == null ? null : Number(row[spec.y]);
      }),
    }));
    charts.push(new Chart(canvas, {type: spec.kind, data: {labels: labels.map((label) => String(label).slice(0, 10)), datasets},
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {legend: {position: "bottom", labels: {boxWidth: 12, usePointStyle: true}}},
        scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 5, maxRotation: 0}},
          y: {beginAtZero: true, grid: {color: "#e9edef"}}}}}));
  }
  const header = document.createElement("tr");
  result.columns.forEach((column) => header.append(element("th", title(column.name))));
  $("columns").replaceChildren(header);
  $("rows").replaceChildren(...result.rows.map((row) => {
    const tr = document.createElement("tr");
    result.columns.forEach((column) => {
      const value = row[column.name];
      tr.append(element("td", value == null ? "NULL" : typeof value === "object" ? JSON.stringify(value) : String(value),
        /INT|FLOAT|NUMERIC|DECIMAL|DOUBLE|BIGINT/.test(column.type) ? "numeric" : ""));
    });
    return tr;
  }));
  $("row-count").textContent = `${result.rows.length} rows${result.truncated ? " / incomplete" : ""}`;
  $("sql").textContent = result.sql;
  $("provenance").replaceChildren(...Object.entries({"Request": result.request_id, "Job": result.job_id,
    "Mode": result.mode, "Scope": result.scope.join(", "), "Bytes scanned": result.bytes_processed,
    "Schema": result.schema_version}).flatMap(([key, value]) => [element("dt", key), element("dd", String(value))]));
}
