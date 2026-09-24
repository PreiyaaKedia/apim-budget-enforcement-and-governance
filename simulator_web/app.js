const state = { users: {}, teams: {}, prices: {}, history: [], budgetMetrics: { period: "", teams: [], users: [] }, analytics: null, activeUser: "", activeView: "overview", activeDashboardTab: "summary", backendMode: "simulation", backendCompatible: true, backendError: "", defaultLimitUsd: null };
const byId = (id) => document.getElementById(id);
const money = (value) => `$${Number(value || 0).toFixed(6)}`;
const integer = (value) => Number(value || 0).toLocaleString();
const milliseconds = (value) => `${integer(Math.round(Number(value || 0)))} ms`;
const percent = (value) => `${Number(value || 0).toFixed(1)}%`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof problem.detail === "string" ? problem.detail : JSON.stringify(problem.detail));
  }
  return response.status === 204 ? null : response.json();
}

async function refresh(preferredUser = state.activeUser) {
  const snapshot = await api("/api/state");
  Object.assign(state, snapshot);
  const userIds = Object.keys(state.users).sort();
  state.activeUser = userIds.includes(preferredUser) ? preferredUser : (userIds[0] || "");
  render();
}

function render() {
  renderMode();
  renderUsers();
  renderTeams();
  renderMetrics();
  renderAnalytics();
  renderPrices();
  renderHistory();
  updateRatePreview();
}

function renderMode() {
  const live = state.backendMode === "live";
  const compatible = state.backendCompatible !== false;
  const metricsView = live && state.activeView === "metrics";
  const dashboardView = live && state.activeView === "dashboard";
  document.body.classList.toggle("live-admin", live);
  byId("backend-status").className = `status ${live ? (compatible ? "live" : "incompatible") : "simulation"}`;
  byId("backend-label").textContent = live ? (compatible ? "Real enforcement" : "Backend update required") : "Local simulation";
  byId("live-probe-fields").hidden = !live;
  byId("simulation-fields").hidden = live;
  byId("default-budget-form").hidden = true;
  byId("team-budget-form").hidden = !live;
  byId("team-budget-note").hidden = !live;
  byId("team-list").hidden = !live;
  byId("budget-form").hidden = live;
  byId("account-list").hidden = live;
  byId("reset-budget").hidden = live;
  byId("rate-preview").hidden = live;
  byId("scenario-panel").hidden = live;
  byId("view-tabs").hidden = !live;
  byId("overview-view").hidden = metricsView || dashboardView;
  byId("metrics-view").hidden = !metricsView;
  byId("dashboard-view").hidden = !dashboardView;
  byId("pricing-panel").hidden = false;
  byId("history-section").hidden = live;
  byId("check-button-label").textContent = live ? "Probe real budget" : "Run simulated check";
  byId("team-budget-note").textContent = compatible
    ? "Every caller assigned to this app role receives an independent monthly allowance."
    : state.backendError;
  for (const control of byId("team-budget-form").elements) control.disabled = live && !compatible;
}

async function refreshAnalytics() {
  const params = new URLSearchParams({
    days: byId("analytics-days").value,
    team: byId("analytics-team").value,
    app_id: byId("analytics-app").value,
    model: byId("analytics-model").value,
  });
  state.analytics = await api(`/api/analytics?${params}`);
  renderAnalytics();
}

function renderAnalytics() {
  const report = state.analytics;
  if (!report) return;
  const summary = report.summary || {};
  byId("analytics-spend").textContent = money(summary.spendUsd);
  byId("analytics-requests").textContent = integer(summary.requests);
  byId("analytics-tokens").textContent = integer(summary.tokens);
  byId("analytics-p95").textContent = milliseconds(summary.p95LatencyMs);
  byId("analytics-error-rate").textContent = percent(summary.errorRate);
  byId("analytics-cache-rate").textContent = percent(summary.cacheReadRate);
  const notice = byId("analytics-notice");
  notice.hidden = report.available && !report.reason && !report.truncated;
  notice.className = `analytics-notice ${report.available ? "empty" : "unavailable"}`;
  notice.textContent = report.truncated
    ? "Showing the latest 20,000 requests. Narrow the filters for complete totals."
    : report.reason || "";
  renderFilterOptions("analytics-team", report.filters?.teams || [], "All teams");
  renderFilterOptions("analytics-app", report.filters?.apps || [], "All applications");
  renderFilterOptions("analytics-model", report.filters?.models || [], "All models");
  renderSpendTrend(report.trends || []);
  renderRanks(report.models || []);
  renderAnalyticsTables(report);
}

function renderFilterOptions(id, values, label) {
  const select = byId(id);
  const selected = select.value;
  select.innerHTML = `<option value="">${label}</option>${values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}`;
  if (values.includes(selected)) select.value = selected;
}

function renderSpendTrend(trends) {
  const maximum = Math.max(...trends.map((item) => Number(item.spendUsd || 0)), 0);
  byId("trend-total").textContent = `${integer(trends.reduce((sum, item) => sum + Number(item.requests || 0), 0))} requests`;
  byId("spend-trend").innerHTML = trends.length
    ? trends.map((item) => `<div class="vertical-bar-item" title="${escapeHtml(item.date)}: ${money(item.spendUsd)}"><div class="vertical-bar" style="height:${maximum ? Math.max(3, Number(item.spendUsd) / maximum * 100) : 0}%"></div><span>${escapeHtml(item.date.slice(5))}</span></div>`).join("")
    : '<div class="empty-row">No spend recorded in this period.</div>';
}

function renderRanks(models) {
  const maximum = Math.max(...models.map((item) => Number(item.spendUsd || 0)), 0);
  byId("model-rank").innerHTML = models.length
    ? models.slice(0, 6).map((item) => `<div class="rank-row"><div><strong>${escapeHtml(item.name)}</strong><span>${integer(item.tokens)} tokens · ${integer(item.requests)} requests</span></div><b>${money(item.spendUsd)}</b><i style="width:${maximum ? Number(item.spendUsd) / maximum * 100 : 0}%"></i></div>`).join("")
    : '<div class="empty-row">Model rankings appear after usage is ingested.</div>';
}

function renderAnalyticsTables(report) {
  byId("analytics-team-body").innerHTML = metricRows(report.teams, "p95LatencyMs", milliseconds);
  byId("analytics-app-body").innerHTML = metricRows(report.apps, "errorRate", percent);
  byId("model-performance-body").innerHTML = report.models?.length
    ? report.models.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td class="mono">${milliseconds(item.averageLatencyMs)}</td><td class="mono">${milliseconds(item.p95LatencyMs)}</td><td class="mono">${percent(item.errorRate)}</td><td class="mono">${money(item.spendUsd)}</td></tr>`).join("")
    : emptyTableRow(5, "No model performance telemetry in this period.");
  byId("analytics-user-body").innerHTML = report.users?.length
    ? report.users.map((item) => `<tr><td><strong title="${escapeHtml(item.identity)}">${escapeHtml(item.identity)}</strong>${item.name ? `<small>${escapeHtml(item.name)}</small>` : ""}</td><td>${escapeHtml(item.teamRole)}</td><td class="mono">${escapeHtml(item.appId)}</td><td class="mono">${integer(item.requests)}</td><td class="mono">${integer(item.tokens)}</td><td class="mono">${money(item.spendUsd)}</td><td class="mono">${milliseconds(item.p95LatencyMs)}</td></tr>`).join("")
    : emptyTableRow(7, "Users appear after APIM usage telemetry is ingested.");
  renderTokenDistribution(report.models || []);
  renderLatencyDistribution(report.latencyDistribution || [], report.summary?.averageLatencyMs);
}

function metricRows(items = [], finalKey, formatter) {
  return items.length
    ? items.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td class="mono">${integer(item.requests)}</td><td class="mono">${integer(item.tokens)}</td><td class="mono">${money(item.spendUsd)}</td><td class="mono">${formatter(item[finalKey])}</td></tr>`).join("")
    : emptyTableRow(5, "No usage telemetry in this period.");
}

function emptyTableRow(columns, message) {
  return `<tr><td colspan="${columns}" class="empty-row">${message}</td></tr>`;
}

function renderTokenDistribution(models) {
  byId("model-token-distribution").innerHTML = models.length
    ? models.map((item) => {
        const mix = item.tokenMix || {};
        const total = Object.values(mix).reduce((sum, value) => sum + Number(value || 0), 0) || 1;
        const segment = (key, label) => `<i class="${key}" style="width:${Number(mix[key] || 0) / total * 100}%" title="${label}: ${integer(mix[key])}"></i>`;
        return `<div class="token-model-row"><div><strong>${escapeHtml(item.name)}</strong><span>${integer(item.tokens)} total · ${money(item.spendUsd)}</span></div><div class="token-stack">${segment("input", "Input")}${segment("output", "Output")}${segment("cacheWrite", "Cache write")}${segment("cacheRead", "Cache read")}</div></div>`;
      }).join("")
    : '<div class="empty-row">Token distribution appears after usage is ingested.</div>';
}

function renderLatencyDistribution(buckets, average) {
  const maximum = Math.max(...buckets.map((bucket) => Number(bucket.requests || 0)), 0);
  byId("average-latency").textContent = `Average ${milliseconds(average)}`;
  byId("latency-distribution").innerHTML = buckets.map((bucket) => `<div class="horizontal-bar-row"><span>${escapeHtml(bucket.label)}</span><div><i style="width:${maximum ? Number(bucket.requests) / maximum * 100 : 0}%"></i></div><b>${integer(bucket.requests)}</b></div>`).join("");
}

function renderMetrics() {
  const report = state.budgetMetrics || { period: "", teams: [], users: [] };
  const totals = report.teams.reduce((result, team) => ({
    allocated: result.allocated + Number(team.allocatedUsd || 0),
    spent: result.spent + Number(team.spentUsd || 0),
    reserved: result.reserved + Number(team.reservedUsd || 0),
    remaining: result.remaining + Number(team.remainingUsd || 0),
  }), { allocated: 0, spent: 0, reserved: 0, remaining: 0 });
  byId("metrics-period").textContent = report.period || "Current month";
  byId("metrics-allocated").textContent = money(totals.allocated);
  byId("metrics-spent").textContent = money(totals.spent);
  byId("metrics-reserved").textContent = money(totals.reserved);
  byId("metrics-remaining").textContent = money(totals.remaining);
  byId("team-metrics-body").innerHTML = report.teams.length
    ? report.teams.map((team) => `<tr><td><strong>${escapeHtml(team.teamRole)}</strong></td><td class="mono">${integer(team.activeUsers)}</td><td class="mono">${money(team.allocatedUsd)}</td><td class="mono">${money(team.spentUsd)}</td><td class="mono">${money(team.reservedUsd)}</td><td class="mono remaining-value">${money(team.remainingUsd)}</td></tr>`).join("")
    : '<tr><td colspan="6" class="empty-row">No team policies are configured.</td></tr>';
  byId("user-metrics-body").innerHTML = report.users.length
    ? report.users.map((user) => `<tr><td><strong title="${escapeHtml(user.userEmail || user.userKey)}">${escapeHtml(user.userEmail || user.userKey)}</strong>${user.userName ? `<small>${escapeHtml(user.userName)}</small>` : ""}</td><td>${escapeHtml(user.teamRole)}</td><td class="mono">${money(user.allocatedUsd)}</td><td class="mono">${money(user.spentUsd)}</td><td class="mono">${money(user.reservedUsd)}</td><td class="mono remaining-value">${money(user.remainingUsd)}</td></tr>`).join("")
    : '<tr><td colspan="6" class="empty-row">Users appear after their first budgeted request this month.</td></tr>';
}

function renderTeams() {
  const teams = Object.values(state.teams || {}).sort((a, b) => a.teamRole.localeCompare(b.teamRole));
  byId("team-list").innerHTML = teams.length
    ? teams.map((team) => `<div class="team-row"><button type="button" data-team="${escapeHtml(team.teamRole)}"><strong>${escapeHtml(team.teamRole)}</strong><small>${money(team.perUserLimitUsd)} per user, monthly</small></button><span>edit →</span></div>`).join("")
    : '<div class="empty-row">Add a team policy to enable its members.</div>';
}

function renderUsers() {
  const ids = Object.keys(state.users).sort();
  byId("user-select").innerHTML = ids.length
    ? ids.map((id) => `<option value="${escapeHtml(id)}" ${id === state.activeUser ? "selected" : ""}>${escapeHtml(id)}</option>`).join("")
    : '<option value="">Create a user budget</option>';
  const user = state.users[state.activeUser];
  byId("limit-metric").textContent = money(user?.limitUsd);
  byId("spent-metric").textContent = money(user?.spentUsd);
  byId("remaining-metric").textContent = money(user?.remainingUsd);
  byId("account-list").innerHTML = ids.length
    ? ids.map((id) => {
        const item = state.users[id];
        return `<div class="list-row"><button type="button" data-user="${escapeHtml(id)}"><strong>${escapeHtml(id)}</strong><small>spent ${money(item.spentUsd)} of ${money(item.limitUsd)}</small></button><span>${money(item.remainingUsd)}</span></div>`;
      }).join("")
    : '<div class="empty-row">Add a user to begin.</div>';
}

function renderPrices() {
  const prices = Object.values(state.prices).sort((a, b) => a.deployment.localeCompare(b.deployment));
  const selected = byId("deployment").value;
  byId("deployment").innerHTML = prices.length
    ? prices.map((item) => `<option value="${escapeHtml(item.deployment)}" ${item.deployment === selected ? "selected" : ""}>${escapeHtml(item.deployment)}</option>`).join("")
    : '<option value="">Add a model rate</option>';
  byId("price-list").innerHTML = prices.length
    ? prices.map((item) => `<div class="list-row"><button type="button" data-price="${escapeHtml(item.deployment)}"><strong>${escapeHtml(item.deployment)}</strong><small>in ${money(item.inputUsdPerMillion)} · out ${money(item.outputUsdPerMillion)} / 1M</small></button><span>edit →</span></div>`).join("")
    : '<div class="empty-row">No model prices yet.</div>';
}

function renderHistory() {
  byId("run-count").textContent = `${state.history.length} scenario${state.history.length === 1 ? "" : "s"}`;
  byId("history-body").innerHTML = state.history.length
    ? state.history.map((item) => `<tr><td><span class="pill ${item.allowed ? "allowed" : "denied"}">${item.allowed ? "ALLOWED" : "DENIED"}</span></td><td>${escapeHtml(item.userId)}</td><td>${escapeHtml(item.deployment)}</td><td class="mono">${integer(item.inputTokens)} / ${integer(item.cacheWriteTokens)} / ${integer(item.cacheReadTokens)} / ${integer(item.outputTokens)}</td><td class="mono">${money(item.reservedUsd)}</td><td class="mono">${money(item.actualUsd)}</td><td class="mono">${money(item.remainingUsd)}</td></tr>`).join("")
    : '<tr><td colspan="7" class="empty-row">Run a scenario to populate the ledger.</td></tr>';
}

function updateRatePreview() {
  const price = state.prices[byId("deployment").value];
  byId("rate-preview").textContent = price
    ? `IN ${money(price.inputUsdPerMillion)} · OUT ${money(price.outputUsdPerMillion)} / 1M`
    : "Select a model";
  if (price) {
    const slider = byId("max-output-tokens");
    slider.max = price.maxOutputTokens;
    if (Number(slider.value) > Number(slider.max)) slider.value = slider.max;
    byId("max-output-value").textContent = integer(slider.value);
  }
}

function openPriceDialog(deployment = "") {
  const price = state.prices[deployment];
  byId("price-deployment").value = price?.deployment || "";
  byId("price-deployment").readOnly = Boolean(price);
  byId("price-input").value = price?.inputUsdPerMillion || "";
  byId("price-cache-write").value = price?.cacheWriteUsdPerMillion || "";
  byId("price-cache-read").value = price?.cacheReadUsdPerMillion || "";
  byId("price-output").value = price?.outputUsdPerMillion || "";
  byId("price-max-output").value = price?.maxOutputTokens || 32768;
  byId("delete-price").hidden = !price;
  byId("price-dialog").showModal();
}

function showDecision(result, isProbe = false) {
  const decision = byId("decision");
  decision.className = `decision ${result.allowed ? "allowed" : "denied"}`;
  decision.querySelector(".decision-mark").textContent = result.allowed ? "✓" : "×";
  byId("decision-label").textContent = result.allowed
    ? (isProbe ? "Reservation allowed and released" : "Request allowed and settled")
    : "Request blocked before inference";
  byId("decision-copy").textContent = result.allowed
    ? (isProbe ? "The real ledger allowed the hold and released it immediately; spend is unchanged." : "Actual usage was charged; unused reservation was returned.")
    : "The conservative reservation exceeds this user's remaining budget.";
  byId("reserved-result").textContent = money(result.reservedUsd);
  byId("actual-result").textContent = money(result.actualUsd);
  byId("released-result").textContent = money(result.releasedUsd);
  byId("remaining-result").textContent = money(result.remainingUsd);
}

function toast(message) {
  const element = byId("toast");
  element.textContent = message;
  element.classList.add("show");
  window.setTimeout(() => element.classList.remove("show"), 2600);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

byId("scenario-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.activeUser) return toast("Create a user budget first.");
  try {
    if (state.backendMode === "live") {
      const result = await api("/api/admin/budget/probe", {
        method: "POST",
        body: JSON.stringify({ email: state.activeUser, amountUsd: byId("probe-amount").value }),
      });
      showDecision({
        ...result,
        reservedUsd: result.amountUsd,
        actualUsd: "0",
        releasedUsd: result.allowed ? result.amountUsd : "0",
      }, true);
      await refresh(state.activeUser);
      return;
    }
    const result = await api("/api/simulations", {
      method: "POST",
      body: JSON.stringify({
        userId: state.activeUser,
        deployment: byId("deployment").value,
        inputTokens: Number(byId("input-tokens").value),
        cacheWriteTokens: Number(byId("cache-write-tokens").value),
        cacheReadTokens: Number(byId("cache-read-tokens").value),
        outputTokens: Number(byId("output-tokens").value),
        requestedMaxOutputTokens: Number(byId("max-output-tokens").value),
      }),
    });
    showDecision(result);
    await refresh(state.activeUser);
  } catch (error) { toast(error.message); }
});

byId("budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = byId("budget-user").value.trim();
  try {
    const path = state.backendMode === "live" ? `/api/admin/budget/users/${encodeURIComponent(userId)}` : `/api/users/${encodeURIComponent(userId)}`;
    await api(path, { method: "PUT", body: JSON.stringify({ limitUsd: byId("budget-limit").value }) });
    byId("budget-user").value = "";
    await refresh(userId);
    toast("Budget saved.");
  } catch (error) { toast(error.message); }
});

byId("team-budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const teamRole = byId("team-role").value.trim();
  try {
    await api(`/api/admin/budget/teams/${encodeURIComponent(teamRole)}`, {
      method: "PUT",
      body: JSON.stringify({
        perUserLimitUsd: byId("team-user-limit").value,
      }),
    });
    await refresh();
    toast("Team policy saved.");
  } catch (error) { toast(error.message); }
});

byId("default-budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/admin/budget/default", { method: "PUT", body: JSON.stringify({ limitUsd: byId("default-budget-limit").value }) });
    await refresh(state.activeUser);
    toast("Default budget saved.");
  } catch (error) { toast(error.message); }
});

byId("price-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const deployment = byId("price-deployment").value.trim();
  const document = {
    id: deployment, deployment, model: "*", modelVersion: "*", region: "local", deploymentType: "Simulation",
    currency: "USD", unit: "usdPerMillionTokens", inputUsdPerMillion: byId("price-input").value,
    cacheWriteUsdPerMillion: byId("price-cache-write").value, cacheReadUsdPerMillion: byId("price-cache-read").value,
    outputUsdPerMillion: byId("price-output").value, maxOutputTokens: Number(byId("price-max-output").value),
    effectiveFrom: new Date().toISOString(), effectiveTo: null, source: "Local simulator",
  };
  try {
    await api(`/api/prices/${encodeURIComponent(deployment)}`, { method: "PUT", body: JSON.stringify(document) });
    byId("price-dialog").close();
    await refresh(state.activeUser);
    byId("deployment").value = deployment;
    updateRatePreview();
    toast("Rate card saved.");
  } catch (error) { toast(error.message); }
});

byId("delete-price").addEventListener("click", async () => {
  const deployment = byId("price-deployment").value;
  try {
    await api(`/api/prices/${encodeURIComponent(deployment)}`, { method: "DELETE" });
    byId("price-dialog").close();
    await refresh(state.activeUser);
    toast("Rate card deleted.");
  } catch (error) { toast(error.message); }
});

byId("reset-budget").addEventListener("click", async () => {
  if (!state.activeUser) return;
  try { await api(`/api/users/${encodeURIComponent(state.activeUser)}/reset`, { method: "POST" }); await refresh(state.activeUser); toast("Spend reset."); }
  catch (error) { toast(error.message); }
});
byId("user-select").addEventListener("change", (event) => { state.activeUser = event.target.value; renderUsers(); });
byId("deployment").addEventListener("change", updateRatePreview);
byId("max-output-tokens").addEventListener("input", (event) => { byId("max-output-value").textContent = integer(event.target.value); });
byId("account-list").addEventListener("click", (event) => { const button = event.target.closest("[data-user]"); if (button) { state.activeUser = button.dataset.user; renderUsers(); } });
byId("team-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-team]");
  if (!button) return;
  const team = state.teams[button.dataset.team];
  byId("team-role").value = team.teamRole;
  byId("team-user-limit").value = team.perUserLimitUsd;
});
byId("price-list").addEventListener("click", (event) => { const button = event.target.closest("[data-price]"); if (button) openPriceDialog(button.dataset.price); });
byId("new-price").addEventListener("click", () => openPriceDialog());
byId("close-dialog").addEventListener("click", () => byId("price-dialog").close());
byId("view-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  state.activeView = button.dataset.view;
  for (const tab of byId("view-tabs").querySelectorAll("button")) tab.classList.toggle("active", tab === button);
  renderMode();
  if (state.activeView === "dashboard" && !state.analytics) refreshAnalytics().catch((error) => toast(error.message));
});
byId("dashboard-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  refreshAnalytics().catch((error) => toast(error.message));
});
byId("dashboard-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-dashboard-tab]");
  if (!button) return;
  state.activeDashboardTab = button.dataset.dashboardTab;
  for (const tab of byId("dashboard-tabs").querySelectorAll("button")) tab.classList.toggle("active", tab === button);
  for (const pane of document.querySelectorAll("[data-dashboard-pane]")) pane.hidden = pane.dataset.dashboardPane !== state.activeDashboardTab;
});

refresh().catch((error) => toast(error.message));