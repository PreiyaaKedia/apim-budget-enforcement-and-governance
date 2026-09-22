const state = { users: {}, prices: {}, history: [], activeUser: "", backendMode: "simulation", defaultLimitUsd: null };
const byId = (id) => document.getElementById(id);
const money = (value) => `$${Number(value || 0).toFixed(6)}`;
const integer = (value) => Number(value || 0).toLocaleString();

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
  renderPrices();
  renderHistory();
  updateRatePreview();
}

function renderMode() {
  const live = state.backendMode === "live";
  document.body.classList.toggle("live-admin", live);
  byId("backend-status").className = `status ${live ? "live" : "simulation"}`;
  byId("backend-label").textContent = live ? "Real enforcement" : "Local simulation";
  byId("live-probe-fields").hidden = !live;
  byId("simulation-fields").hidden = live;
  byId("default-budget-form").hidden = !live;
  byId("reset-budget").hidden = live;
  byId("rate-preview").hidden = live;
  byId("scenario-panel").hidden = live;
  byId("pricing-panel").hidden = false;
  byId("history-section").hidden = live;
  byId("check-button-label").textContent = live ? "Probe real budget" : "Run simulated check";
  if (live && state.defaultLimitUsd !== null) byId("default-budget-limit").value = state.defaultLimitUsd;
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
byId("price-list").addEventListener("click", (event) => { const button = event.target.closest("[data-price]"); if (button) openPriceDialog(button.dataset.price); });
byId("new-price").addEventListener("click", () => openPriceDialog());
byId("close-dialog").addEventListener("click", () => byId("price-dialog").close());

refresh().catch((error) => toast(error.message));