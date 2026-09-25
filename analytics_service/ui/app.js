"use strict";
const $ = (id) => document.getElementById(id);
let signedIn = false;
let library = {saved: [], frequent: [], history: []};
const sessionChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("aml-session") : null;
let charts = [];
let generation = 0;
let currentResult = null;
let page = 0;
const PAGE_SIZE = 25;
const markets = {HASE_HK: "Hong Kong", HSBC_GB: "United Kingdom", HSBC_IN: "India", HSBC_TW: "Taiwan", HSBC_FR: "France", HSBC_PL: "Poland", HSBC_IE: "Ireland"};
const title = (name) => String(name).replaceAll("_", " ").replace(/\b(id|usd|sar|aml)\b/gi, (v) => v.toUpperCase()).replace(/^./, (v) => v.toUpperCase());
const marketNames = (scope) => scope.map((name) => markets[name] || name).join(" · ");
const numeric = (column) => /^(INTEGER|INT64|FLOAT|FLOAT64|NUMERIC|BIGNUMERIC|DECIMAL|DOUBLE|BIGINT)$/.test(column.type) && column.mode !== "REPEATED";
function formatValue(value, unit = "value") {
  if (value == null) return "Not available";
  if (typeof value === "object") return JSON.stringify(value);
  const text = String(value);
  // Group decimal strings without passing exact BigQuery NUMERIC values through Number.
  if (!/^-?\d+(\.\d+)?$/.test(text)) return text;
  const [whole, fraction] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? "." + fraction : "");
  return unit === "USD" ? "$" + grouped : grouped;
}
function chartLabel(value) {
  const words = String(value ?? "Not available").replaceAll("_", " ").split(/\s+/);
  const lines = [""];
  for (const word of words) {
    const chunks = word.match(/.{1,18}/g) || [""];
    for (const chunk of chunks) {
      const last = lines.length - 1;
      if (lines[last] && lines[last].length + chunk.length + 1 > 18) lines.push(chunk);
      else lines[last] += (lines[last] ? " " : "") + chunk;
    }
  }
  return lines;
}
function setView(view) {
  for (const name of ["overview", "table"]) {
    const selected = name === view;
    $(name + "-tab").setAttribute("aria-selected", String(selected));
    $(name + "-tab").tabIndex = selected ? 0 : -1;
    $(name + "-panel").hidden = !selected;
  }
  if (view === "overview") charts.forEach((chart) => chart.resize());
}
for (const name of ["overview", "table"]) {
  $(name + "-tab").addEventListener("click", () => setView(name));
  $(name + "-tab").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || $("overview-tab").hidden) return;
    event.preventDefault();
    const target = event.key === "Home" ? "overview" : event.key === "End" ? "table" : name === "overview" ? "table" : "overview";
    setView(target); $(target + "-tab").focus();
  });
}
function renderTable() {
  if (!currentResult) return;
  const result = currentResult;
  $("rows").replaceChildren(...result.rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((row) => {
    const tr = element("tr");
    result.columns.forEach((column) => {
      const value = row[column.name];
      tr.append(element("td", numeric(column) ? formatValue(value) : value == null ? "Not available" : typeof value === "object" ? JSON.stringify(value) : String(value), numeric(column) ? "numeric" : ""));
    });
    return tr;
  }));
  $("page-label").textContent = result.rows.length ? `${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, result.rows.length)} of ${result.rows.length.toLocaleString()}` : "No matching records";
  $("previous-page").disabled = page === 0;
  $("next-page").disabled = (page + 1) * PAGE_SIZE >= result.rows.length;
  document.querySelector(".table-wrap").scrollTop = 0;
}
$("previous-page").addEventListener("click", () => { if (page > 0) { page--; renderTable(); } });
$("next-page").addEventListener("click", () => { if (currentResult && (page + 1) * PAGE_SIZE < currentResult.rows.length) { page++; renderTable(); } });
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
async function api(path, body, method) {
  let response;
  try { response = await fetch(path, {
    method: method || (body ? "POST" : "GET"), credentials: "same-origin",
    headers: {"X-AML-Request": "1", ...(body ? {"Content-Type": "application/json"} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}),
  }); } catch (_) { throw new Error("The service could not be reached. Please try again shortly."); }
  const result = await response.json();
  if (!response.ok) {
    const messages = {
      unauthorized: "Your session is no longer valid. Please sign in again.",
      invalid_login: "Username or password is incorrect.",
      scan_budget: "This request covers too much data. Try a shorter reporting period or a narrower scope.",
      historical_ambiguity: "Customers have multiple historical records. Specify the latest record per customer and a reporting date.",
      clarification_required: "Please specify the measure, markets and reporting period you want to analyze.",
      resource_denied: "You do not have the required access to answer this question. Check your profile for permitted tables or contact your administrator.",
      model_timeout: "The analysis took too long. Try a more focused question.",
      query_timeout: "The data request took too long. No results are available. Try a shorter reporting period.",
      rate_limit: "Too many requests. Please wait a moment before trying again.",
      busy: "Another analysis is in progress. Please try again shortly.",
    };
    const code = result.error?.code;
    const error = new Error(messages[code] || (response.status >= 500 ? "Analysis is temporarily unavailable. Please try again shortly." : "We could not safely answer that question. Try specifying a measure, market and reporting period."));
    error.code = code;
    throw error;
  }
  return result;
}
function clearResults() {
  currentResult = null;
  page = 0;
  charts.forEach((chart) => chart.destroy());
  charts = [];
  for (const id of ["kpis", "charts", "rows", "columns", "sql", "provenance", "warnings", "insights", "detailed-warnings", "result-context"]) {
    $(id).replaceChildren();
  }
  $("results").hidden = true;
  $("empty").hidden = false;
  $("loading-indicator").hidden = true;
  $("empty").setAttribute("aria-busy", "false");
  document.querySelector(".provenance").open = false;
}
function setLibraryView(name) {
  for (const value of ["faq", "history"]) {
    const selected = value === name;
    $(value + "-tab").setAttribute("aria-selected", String(selected));
    $(value + "-tab").tabIndex = selected ? 0 : -1;
    $(value + "-panel").hidden = !selected;
  }
  $("clear-history").hidden = name !== "history" || !library.history.length;
}
for (const name of ["faq", "history"]) {
  $(name + "-tab").addEventListener("click", () => setLibraryView(name));
  $(name + "-tab").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? "faq" : event.key === "End" ? "history" : name === "faq" ? "history" : "faq";
    setLibraryView(target); $(target + "-tab").focus();
  });
}
function renderLibrary() {
  updateSaveButton();
  for (const [id, entries] of [["saved-questions", library.saved.map(question => ({question}))], ["frequent-questions", library.frequent], ["history-questions", library.history]]) {
    $(id).replaceChildren();
    if (!entries.length) $(id).append(element("p", id === "saved-questions" ? "No saved questions yet" : id === "history-questions" ? "No previous queries" : "No completed queries yet", "library-empty"));
    for (const entry of entries) {
      const row = element("div", undefined, "library-row");
      const pick = element("button", entry.question, "question-link");
      pick.type = "button"; pick.title = "Use this question";
      pick.addEventListener("click", () => { if (!$("question").disabled) { $("question").value = entry.question; updateSaveButton(); $("question").focus(); } });
      row.append(pick);
      if (entry.count) row.append(element("span", `${entry.count} ${entry.count === 1 ? "query" : "queries"}`, "library-meta"));
      if (entry.created) row.append(element("span", `${new Date(entry.created).toLocaleString()} · ${entry.outcome === "completed" ? "Completed" : "Not completed"}`, "library-meta"));
      const saved = library.saved.includes(entry.question);
      const save = element("button", saved ? "−" : "+", "save-toggle");
      save.type = "button"; save.title = saved ? "Remove saved question" : "Save question to FAQ";
      save.setAttribute("aria-label", save.title + ": " + entry.question);
      save.addEventListener("click", () => saveQuestion(entry.question, !saved));
      row.append(save); $(id).append(row);
    }
  }
  $("clear-history").hidden = $("history-panel").hidden || !library.history.length;
}
async function refreshLibrary() {
  const version = generation;
  try { const data = await api("/workspace"); if (version === generation && signedIn) { library = data; renderLibrary(); } }
  catch (_) { if (version === generation && signedIn) $("message").textContent = "Question history is temporarily unavailable."; }
}
async function saveQuestion(question, saved = true) {
  if (!question.trim()) { $("message").textContent = "Enter a question to save."; return; }
  const version = generation;
  try { const data = await api("/workspace/saved", {question: question.trim(), saved}); if (version === generation && signedIn) { library = data; renderLibrary(); $("message").textContent = ""; } }
  catch (error) { if (version === generation) $("message").textContent = error.message; }
}
function updateSaveButton() {
  const saved = library.saved.includes($("question").value.trim());
  $("save-question").textContent = saved ? "−" : "+";
  $("save-question").title = saved ? "Remove saved question" : "Save question to FAQ";
  $("save-question").setAttribute("aria-label", $("save-question").title);
}
$("question").addEventListener("input", updateSaveButton);
$("save-question").addEventListener("click", () => saveQuestion($("question").value, !library.saved.includes($("question").value.trim())));
$("clear-history").addEventListener("click", async () => {
  const version = generation;
  try { const data = await api("/workspace/history", null, "DELETE"); if (version === generation) { library = data; renderLibrary(); } }
  catch (error) { if (version === generation) $("message").textContent = error.message; }
});
async function connect(version) {
  const response = await api("/status");
  if (version !== generation) return;
  signedIn = true;
  const ready = response.model.ready && response.bigquery.ready;
  for (const id of ["question", "run"]) $(id).disabled = !ready;
  $("save-question").disabled = false;
  $("run").textContent = "Analyze";
  $("scope").textContent = marketNames(response.scope);
  $("status").textContent = ready ? "Connected" : "Service unavailable";
  $("profile-link").textContent = `${title(response.profile.role)} · ${response.profile.username}`;
  $("question-library").hidden = false;
  $("empty-title").textContent = "Your next insight starts here";
  $("empty-status").textContent = ready ? "Ready for your question" : "Analysis is temporarily unavailable. Please sign in again shortly.";
  await refreshLibrary();
}
function disconnect() {
  generation += 1;
  signedIn = false;
  library = {saved: [], frequent: [], history: []};
  renderLibrary();
  clearResults();
  for (const id of ["question", "run", "save-question"]) $(id).disabled = true;
  $("question").value = "";
  $("run").textContent = "Analyze";
  $("status").textContent = "Sign in required";
  $("scope").textContent = "Not signed in";
  $("empty-title").textContent = "Your next insight starts here";
  $("empty-status").textContent = "Sign in to begin your analysis";
  $("question-library").hidden = true;
  $("question-library").open = false;
  setLibraryView("faq");
  $("profile-link").textContent = "Your profile";
}
function connectionError(error) {
  disconnect();
  if (error.code === "unauthorized") location.replace("/login");
  else $("message").textContent = "Analysis is temporarily unavailable. Please refresh to try again.";
}
if (sessionChannel) sessionChannel.onmessage = () => { disconnect(); connect(generation).catch(connectionError); };
// HttpOnly session cookies restore an existing session; no credentials enter browser storage.
connect(generation).catch(connectionError);
$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const thisGeneration = ++generation;
  clearResults();
  $("run").disabled = true;
  $("run").textContent = "Analyzing...";
  $("question").disabled = true;
  $("message").textContent = "";
  $("empty-title").textContent = "Preparing your analysis";
  $("empty-status").textContent = "This may take a few minutes. Your question is being checked against the available data.";
  $("loading-indicator").hidden = false;
  $("empty").setAttribute("aria-busy", "true");
  try {
    const result = await api("/query", {question: $("question").value});
    if (thisGeneration !== generation) return;
    render(result);
  } catch (error) {
    if (thisGeneration !== generation) return;
    if (error.code === "unauthorized") { disconnect(); location.replace("/login"); return; }
    $("message").textContent = error.message;
    $("empty-title").textContent = "Analysis not completed";
    $("empty-status").textContent = "No results to display. Refine your question and try again.";
  } finally {
    if (thisGeneration === generation) {
      $("run").textContent = "Analyze";
      $("run").disabled = !signedIn;
      $("question").disabled = !signedIn;
      $("loading-indicator").hidden = true;
      $("empty").setAttribute("aria-busy", "false");
      if (signedIn) await refreshLibrary();
    }
  }
});
function render(result) {
  currentResult = result;
  page = 0;
  $("empty").hidden = true;
  $("results").hidden = false;
  $("metric-title").textContent = result.question || title(result.metric);
  $("status").textContent = result.mode === "offline_fixture" ? "Offline demonstration" : "Connected";
  $("freshness").textContent = `Data as of ${new Date(result.data_as_of).toLocaleDateString("en-GB", {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"})}`;
  const routine = new Set(["Synthetic demo data; risk outputs are simulated.", "KPI definitions require domain-owner approval before real banking use.", "AI-generated SQL passed schema, access and execution checks. These checks do not prove it matches your intended business meaning."]);
  const important = result.warnings.filter((warning) => !routine.has(warning));
  if (result.truncated) important.unshift("Incomplete result: only part of the data is shown. Narrow your question before drawing conclusions.");
  if (result.mode === "offline_fixture") important.unshift("Offline demonstration results, not connected data.");
  $("warnings").replaceChildren(...important.map((warning) => element("div", warning)));
  $("detailed-warnings").replaceChildren(...result.warnings.map((warning) => element("div", warning)));
  $("result-context").textContent = `Access scope: ${marketNames(result.scope)} · ${result.timezone} reporting`;
  $("insights").replaceChildren(...result.dashboard.insights.filter((insight) => !insight.startsWith("BigQuery returned") && !insight.includes("query results table") && !insight.includes("dimensions do not form") && !insight.includes("Result limit reached")).map((insight) => element("li", insight.replace("No matching records were returned by BigQuery.", "No matching records for this question."))));
  $("kpis").replaceChildren();
  $("charts").replaceChildren();
  const colors = ["#168477", "#a71d32", "#d3a53b", "#667c89"];
  for (const spec of result.truncated ? [] : result.dashboard.charts) {
    if (spec.kind === "kpi") {
      const node = element("div", undefined, "kpi");
      node.append(element("div", title(spec.measure), "kpi-label"),
        element("div", formatValue(spec.value, spec.unit), "kpi-value"), element("div", spec.unit === "value" ? "" : spec.unit, "kpi-unit"));
      $("kpis").append(node);
      continue;
    }
    const pane = element("div", undefined, "chart");
    const wrap = element("div", undefined, "canvas-wrap");
    const canvas = document.createElement("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${title(spec.y)} by ${title(spec.x)}; values are in the query results table`);
    wrap.append(canvas);
    pane.append(element("h3", `${title(spec.y)}${["value", "count"].includes(spec.unit) ? "" : " · " + spec.unit}`), wrap);
    $("charts").append(pane);
    const labels = [...new Set(result.rows.map((row) => row[spec.x]))];
    if (spec.kind === "line") labels.sort();
    const groups = spec.series ? [...new Set(result.rows.map((row) => row[spec.series]))] : [null];
    const datasets = groups.map((group, index) => ({
      label: group == null ? title(spec.y) : title(group), borderColor: colors[index % colors.length],
      backgroundColor: colors[index % colors.length], borderWidth: 2, pointRadius: 3,
      spanGaps: false, data: labels.map((label) => {
        const row = result.rows.find((value) => value[spec.x] === label && (!spec.series || value[spec.series] === group));
        return row?.[spec.y] == null ? null : Number(row[spec.y]);
      }),
    }));
    const horizontal = spec.kind === "bar";
    const displayLabels = labels.map(label => horizontal ? chartLabel(label) : label == null ? "Not available" : String(label));
    if (horizontal) wrap.style.height = `${Math.max(240, labels.length * Math.max(groups.length * 28, Math.max(...displayLabels.map(label => label.length)) * 14 + 14) + 65)}px`;
    charts.push(new Chart(canvas, {type: spec.kind, data: {labels: displayLabels, datasets},
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        indexAxis: horizontal ? "y" : "x",
        plugins: {legend: {display: Boolean(spec.series), position: "bottom", labels: {boxWidth: 12, usePointStyle: true}},
          tooltip: {callbacks: {label: (context) => `${context.dataset.label}: ${formatValue(context.raw, spec.unit)}${spec.unit === "ratio" ? " ratio" : ""}`}}},
        scales: {x: {beginAtZero: horizontal, grid: {display: horizontal, color: "#e7ecea"}, ticks: {maxTicksLimit: horizontal ? 6 : 8, maxRotation: 0}},
          y: {beginAtZero: !horizontal, grid: {display: !horizontal, color: "#e7ecea"}}}}}));
  }
  const header = document.createElement("tr");
  result.columns.forEach((column) => {
    const unit = result.units?.[column.name];
    const th = element("th", title(column.name) + (unit && !["value", "count"].includes(unit) ? ` (${unit})` : ""), numeric(column) ? "numeric" : "");
    th.scope = "col"; header.append(th);
  });
  $("columns").replaceChildren(header);
  renderTable();
  $("row-count").textContent = `${result.rows.length.toLocaleString()} result${result.rows.length === 1 ? "" : "s"}${result.truncated ? " · incomplete" : ""}`;
  $("overview-tab").hidden = !result.dashboard.charts.length || result.truncated;
  setView($("overview-tab").hidden ? "table" : "overview");
  $("sql").textContent = result.sql;
  $("provenance").replaceChildren(...Object.entries({"Request": result.request_id, "Job": result.job_id,
    "Mode": result.mode, "Scope": result.scope.join(", "), "Bytes scanned": result.bytes_processed,
    "Schema": result.schema_version}).flatMap(([key, value]) => [element("dt", key), element("dd", String(value))]));
}
