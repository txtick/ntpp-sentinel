const state = {
  view: "home",
  actor: localStorage.getItem("ntpp.actor") || "",
  secret: localStorage.getItem("ntpp.secret") || "",
  selections: {
    alertId: null,
    customerId: null,
    techId: null,
    reminderId: null,
  },
  data: {
    home: null,
    alerts: null,
    customers: null,
    technicians: null,
    reminders: null,
  },
};

const els = {
  mainPanel: document.getElementById("main-panel"),
  detailPanel: document.getElementById("detail-panel"),
  viewTitle: document.getElementById("view-title"),
  viewKicker: document.getElementById("view-kicker"),
  statusPill: document.getElementById("status-pill"),
  filters: document.getElementById("view-filters"),
  actorInput: document.getElementById("actor-input"),
  secretInput: document.getElementById("secret-input"),
  toast: document.getElementById("toast"),
};

const viewMeta = {
  home: { kicker: "Home", title: "Dashboard Overview" },
  alerts: { kicker: "Alerts", title: "Tracked Alert Queue" },
  customers: { kicker: "Customers", title: "Customer Operations View" },
  technicians: { kicker: "Technicians", title: "Field Operator Snapshot" },
  reminders: { kicker: "Reminders", title: "Follow-Up Queue" },
};

const filters = {
  alerts: { status: "", category: "", limit: 40 },
  customers: { search: "", operational_only: 1, status: "", limit: 50 },
  technicians: {
    search: "",
    active_only: 0,
    with_current_assignments_only: 1,
    with_recent_route_activity_only: 0,
    field_only: 0,
    role_type: "",
    limit: 40,
  },
  reminders: { status: "", assigned_to: "", source_type: "", overdue_only: 0, search: "", limit: 40 },
};

function init() {
  els.actorInput.value = state.actor;
  els.secretInput.value = state.secret;

  document.querySelectorAll(".nav-link").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });

  document.getElementById("save-auth").addEventListener("click", () => {
    state.actor = els.actorInput.value.trim();
    state.secret = els.secretInput.value.trim();
    localStorage.setItem("ntpp.actor", state.actor);
    localStorage.setItem("ntpp.secret", state.secret);
    showToast("Operator session saved.");
  });

  document.getElementById("refresh-view").addEventListener("click", () => loadCurrentView(true));

  setView("home");
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-link").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
  });
  const meta = viewMeta[view];
  els.viewKicker.textContent = meta.kicker;
  els.viewTitle.textContent = meta.title;
  renderFilters();
  loadCurrentView(true);
}

function setStatus(text, type = "info") {
  els.statusPill.textContent = text;
  els.statusPill.className = `pill pill-${type}`;
}

function showToast(message, timeout = 2800) {
  els.toast.hidden = false;
  els.toast.textContent = message;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => {
    els.toast.hidden = true;
  }, timeout);
}

async function api(path, { method = "GET", auth = false } = {}) {
  const headers = {};
  if (auth) {
    if (!state.secret) throw new Error("Save an API secret first to run queue actions.");
    headers["X-NTPP-Secret"] = state.secret;
  }
  const response = await fetch(path, { method, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail ? JSON.stringify(data.detail) : `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return data;
}

function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "" || value === 0 || value === false) return;
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : "";
}

function safeName(...values) {
  return values.find((value) => value && String(value).trim()) || "Untitled";
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function toDatetimeLocalValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function toUtcIso(localValue) {
  if (!localValue) return "";
  const date = new Date(localValue);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

function badge(value, typeHint = "") {
  const normalized = String(value || typeHint || "muted").toLowerCase().replace(/\s+/g, "-");
  const klass = [
    "critical",
    "high",
    "warning",
    "normal",
    "open",
    "acknowledged",
    "resolved",
    "completed",
    "snoozed",
    "canceled",
    "cleared",
  ].includes(normalized)
    ? normalized
    : "muted";
  return `<span class="pill pill-${klass}">${escapeHtml(String(value || "unknown"))}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderFilters() {
  if (state.view === "home") {
    els.filters.innerHTML = `<div class="muted">Live snapshot of backend summary, recent alerts, and reminder pressure.</div>`;
    return;
  }

  if (state.view === "alerts") {
    const f = filters.alerts;
    els.filters.innerHTML = `
      <label class="filter-chip"><span>Status</span>
        <select id="filter-alert-status">
          <option value="">All</option>
          <option value="open" ${f.status === "open" ? "selected" : ""}>Open</option>
          <option value="acknowledged" ${f.status === "acknowledged" ? "selected" : ""}>Acknowledged</option>
          <option value="snoozed" ${f.status === "snoozed" ? "selected" : ""}>Snoozed</option>
          <option value="resolved" ${f.status === "resolved" ? "selected" : ""}>Resolved</option>
          <option value="cleared" ${f.status === "cleared" ? "selected" : ""}>Cleared</option>
        </select>
      </label>
      <label class="filter-chip"><span>Category</span>
        <select id="filter-alert-category">
          <option value="">All</option>
          <option value="pool" ${f.category === "pool" ? "selected" : ""}>Pool</option>
          <option value="process" ${f.category === "process" ? "selected" : ""}>Process</option>
          <option value="revenue" ${f.category === "revenue" ? "selected" : ""}>Revenue</option>
        </select>
      </label>
    `;
    document.getElementById("filter-alert-status").onchange = (e) => {
      filters.alerts.status = e.target.value;
      loadAlerts(true);
    };
    document.getElementById("filter-alert-category").onchange = (e) => {
      filters.alerts.category = e.target.value;
      loadAlerts(true);
    };
    return;
  }

  if (state.view === "customers") {
    const f = filters.customers;
    els.filters.innerHTML = `
      <label class="filter-chip"><span>Search</span><input id="filter-customer-search" value="${escapeHtml(f.search)}" placeholder="Name, email, or phone" /></label>
      <label class="filter-chip"><span>Status</span>
        <select id="filter-customer-status">
          <option value="">All</option>
          <option value="active" ${f.status === "active" ? "selected" : ""}>Active</option>
          <option value="inactive" ${f.status === "inactive" ? "selected" : ""}>Inactive</option>
          <option value="lead" ${f.status === "lead" ? "selected" : ""}>Lead</option>
        </select>
      </label>
      <label class="filter-chip"><span>Operational Only</span>
        <select id="filter-customer-operational">
          <option value="1" ${f.operational_only ? "selected" : ""}>Yes</option>
          <option value="0" ${!f.operational_only ? "selected" : ""}>No</option>
        </select>
      </label>
    `;
    document.getElementById("filter-customer-search").onchange = (e) => {
      filters.customers.search = e.target.value.trim();
      loadCustomers(true);
    };
    document.getElementById("filter-customer-status").onchange = (e) => {
      filters.customers.status = e.target.value;
      loadCustomers(true);
    };
    document.getElementById("filter-customer-operational").onchange = (e) => {
      filters.customers.operational_only = Number(e.target.value);
      loadCustomers(true);
    };
    return;
  }

  if (state.view === "technicians") {
    const f = filters.technicians;
    els.filters.innerHTML = `
      <label class="filter-chip"><span>Search</span><input id="filter-tech-search" value="${escapeHtml(f.search)}" placeholder="Name or tech id" /></label>
      <label class="filter-chip"><span>Current Assignments</span>
        <select id="filter-tech-current">
          <option value="1" ${f.with_current_assignments_only ? "selected" : ""}>Yes</option>
          <option value="0" ${!f.with_current_assignments_only ? "selected" : ""}>No</option>
        </select>
      </label>
      <label class="filter-chip"><span>Recent Route Activity</span>
        <select id="filter-tech-recent">
          <option value="0" ${!f.with_recent_route_activity_only ? "selected" : ""}>Any</option>
          <option value="1" ${f.with_recent_route_activity_only ? "selected" : ""}>Only Recent</option>
        </select>
      </label>
      <label class="filter-chip"><span>Role</span><input id="filter-tech-role" value="${escapeHtml(f.role_type)}" placeholder="Tech, Admin, Owner" /></label>
    `;
    document.getElementById("filter-tech-search").onchange = (e) => {
      filters.technicians.search = e.target.value.trim();
      loadTechnicians(true);
    };
    document.getElementById("filter-tech-current").onchange = (e) => {
      filters.technicians.with_current_assignments_only = Number(e.target.value);
      loadTechnicians(true);
    };
    document.getElementById("filter-tech-recent").onchange = (e) => {
      filters.technicians.with_recent_route_activity_only = Number(e.target.value);
      loadTechnicians(true);
    };
    document.getElementById("filter-tech-role").onchange = (e) => {
      filters.technicians.role_type = e.target.value.trim();
      loadTechnicians(true);
    };
    return;
  }

  if (state.view === "reminders") {
    const f = filters.reminders;
    els.filters.innerHTML = `
      <label class="filter-chip"><span>Status</span>
        <select id="filter-reminder-status">
          <option value="">All</option>
          <option value="open" ${f.status === "open" ? "selected" : ""}>Open</option>
          <option value="acknowledged" ${f.status === "acknowledged" ? "selected" : ""}>Acknowledged</option>
          <option value="snoozed" ${f.status === "snoozed" ? "selected" : ""}>Snoozed</option>
          <option value="completed" ${f.status === "completed" ? "selected" : ""}>Completed</option>
          <option value="canceled" ${f.status === "canceled" ? "selected" : ""}>Canceled</option>
        </select>
      </label>
      <label class="filter-chip"><span>Assigned To</span><input id="filter-reminder-assigned" value="${escapeHtml(f.assigned_to)}" placeholder="jarrett" /></label>
      <label class="filter-chip"><span>Search</span><input id="filter-reminder-search" value="${escapeHtml(f.search)}" placeholder="Customer or reminder title" /></label>
      <label class="filter-chip"><span>Overdue Only</span>
        <select id="filter-reminder-overdue">
          <option value="0" ${!f.overdue_only ? "selected" : ""}>No</option>
          <option value="1" ${f.overdue_only ? "selected" : ""}>Yes</option>
        </select>
      </label>
    `;
    document.getElementById("filter-reminder-status").onchange = (e) => {
      filters.reminders.status = e.target.value;
      loadReminders(true);
    };
    document.getElementById("filter-reminder-assigned").onchange = (e) => {
      filters.reminders.assigned_to = e.target.value.trim();
      loadReminders(true);
    };
    document.getElementById("filter-reminder-search").onchange = (e) => {
      filters.reminders.search = e.target.value.trim();
      loadReminders(true);
    };
    document.getElementById("filter-reminder-overdue").onchange = (e) => {
      filters.reminders.overdue_only = Number(e.target.value);
      loadReminders(true);
    };
  }
}

async function loadCurrentView(force = false) {
  try {
    setStatus("Loading…", "warning");
    if (state.view === "home") await loadHome(force);
    if (state.view === "alerts") await loadAlerts(force);
    if (state.view === "customers") await loadCustomers(force);
    if (state.view === "technicians") await loadTechnicians(force);
    if (state.view === "reminders") await loadReminders(force);
    setStatus("Live", "info");
  } catch (error) {
    setStatus("Error", "critical");
    els.mainPanel.innerHTML = `<div class="empty-state">Could not load this view.<br /><br />${escapeHtml(error.message)}</div>`;
    showToast(error.message, 4400);
  }
}

async function loadHome() {
  const [summary, alerts, reminders] = await Promise.all([
    api("/api/home/summary"),
    api("/api/alerts?limit=6"),
    api("/api/reminders?limit=6"),
  ]);
  state.data.home = { summary, alerts, reminders };
  renderHome();
}

function renderHome() {
  const payload = state.data.home.summary.summary || {};
  const alertCounts = payload.tracked_alert_counts_by_status || [];
  const reminderCounts = payload.reminder_counts || {};
  const cards = [
    ["Active Customers", payload.active_customer_count],
    ["Active Pools", payload.active_pool_count],
    ["Customers With Current Alerts", payload.customers_with_current_alerts],
    ["Critical Current Alerts", payload.critical_current_alert_count],
    ["Trend Alerts", payload.chemistry_trend_alert_count],
    ["Revenue Opportunities", payload.revenue_opportunity_count],
    ["Tracked Open Reminders", reminderCounts.open_reminder_count],
    ["Overdue Reminders", reminderCounts.overdue_reminder_count],
  ];

  els.mainPanel.innerHTML = `
    <div class="stat-grid">
      ${cards.map(([label, value]) => `<article class="stat-card"><span class="muted">${label}</span><strong>${escapeHtml(value ?? "0")}</strong></article>`).join("")}
    </div>
    <section class="section-card">
      <h3>Tracked Alert Status Mix</h3>
      <p class="panel-subtitle">Durable workflow state from the web backend, not raw query output.</p>
      <div class="list-grid">
        ${alertCounts.map((item) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(item.category)}</strong>${badge(item.status)}</div><div class="muted">${escapeHtml(item.count)}</div></div>`).join("") || `<div class="empty-state">No tracked alerts yet.</div>`}
      </div>
    </section>
    <section class="section-card">
      <h3>Recent Alerts</h3>
      <div class="item-list">
        ${state.data.home.alerts.items.map((item) => itemCard(item.title, `${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}`, formatDateTime(item.last_detected_at))).join("")}
      </div>
    </section>
    <section class="section-card">
      <h3>Reminder Pressure</h3>
      <div class="item-list">
        ${state.data.home.reminders.items.map((item) => itemCard(item.title, `${badge(item.status)} ${item.assigned_to ? `<span class="dense">${escapeHtml(item.assigned_to)}</span>` : ""}`, item.due_at ? `Due ${formatDateTime(item.due_at)}` : "No due date")).join("") || `<div class="empty-state">No reminders in queue.</div>`}
      </div>
    </section>
  `;

  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>Pipeline Pulse</h3>
        <div class="meta-stack">
          <div class="meta-row"><span>Generated</span><strong>${formatDateTime(payload.generated_at)}</strong></div>
          <div class="meta-row"><span>Last Successful Ingest</span><strong>${formatDateTime(payload.last_successful_pipeline_at)}</strong></div>
          <div class="meta-row"><span>Reminder Counts</span><strong>${escapeHtml(JSON.stringify(reminderCounts))}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>What’s Ready</h3>
        <p class="muted">This frontend is already talking to the tracked backend queues. Alerts and reminders both support operator actions from the API.</p>
      </section>
    </div>
  `;
}

async function loadAlerts() {
  const result = await api(`/api/alerts${qs(filters.alerts)}`);
  state.data.alerts = result;
  if (!state.selections.alertId && result.items[0]) state.selections.alertId = result.items[0].id;
  renderAlerts();
  if (state.selections.alertId) await loadAlertDetail(state.selections.alertId);
}

function renderAlerts() {
  const result = state.data.alerts;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Alert Queue</h3>
      <p class="panel-subtitle">${escapeHtml(result.total)} tracked items in this filter set.</p>
      <div class="item-list">
        ${result.items.map((item) => `
          <article class="item-card ${state.selections.alertId === item.id ? "is-selected" : ""}" data-alert-id="${item.id}">
            <div class="item-card-header">
              <div>
                <h4>${escapeHtml(item.title)}</h4>
                <div class="muted">${escapeHtml(item.summary || "")}</div>
              </div>
              <div class="meta-stack">${badge(item.severity)} ${badge(item.status)}</div>
            </div>
            <div class="meta-row">
              <span>${escapeHtml(item.category)} · rule ${escapeHtml(item.rule_code)}</span>
              <span>${formatDateTime(item.last_detected_at)}</span>
            </div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  els.mainPanel.querySelectorAll("[data-alert-id]").forEach((el) => {
    el.addEventListener("click", async () => {
      state.selections.alertId = Number(el.dataset.alertId);
      renderAlerts();
      await loadAlertDetail(state.selections.alertId);
    });
  });
}

async function loadAlertDetail(alertId) {
  const detail = await api(`/api/alerts/${alertId}`);
  renderAlertDetail(detail);
}

function renderAlertDetail(detail) {
  const item = detail.item;
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-header">
          <div>
            <h3>${escapeHtml(item.title)}</h3>
            <p class="muted">${escapeHtml(item.summary || "")}</p>
          </div>
          <div class="meta-stack">${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}</div>
        </div>
        <div class="meta-stack">
          <div class="meta-row"><span>Customer</span><strong>${escapeHtml(item.metadata_json?.customer_name || item.customer_id || "—")}</strong></div>
          <div class="meta-row"><span>Pool</span><strong>${escapeHtml(item.metadata_json?.pool_name || item.pool_id || "—")}</strong></div>
          <div class="meta-row"><span>Last Detected</span><strong>${formatDateTime(item.last_detected_at)}</strong></div>
          <div class="meta-row"><span>Snoozed Until</span><strong>${formatDateTime(item.snoozed_until)}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Alert Actions</h3>
        <div class="detail-actions">
          <button class="button button-secondary" data-alert-action="ack">Acknowledge</button>
          <button class="button button-danger" data-alert-action="resolve">Resolve</button>
        </div>
        <div class="split">
          <label><span>Snooze Until</span><input id="alert-snooze-until" type="datetime-local" /></label>
          <label><span>Snooze Note</span><textarea id="alert-snooze-note" placeholder="Waiting for the next route or customer callback"></textarea></label>
          <button class="button button-secondary" id="alert-snooze-button">Snooze Alert</button>
        </div>
      </section>
      <section class="detail-card">
        <h3>Create Reminder</h3>
        <div class="split">
          <label><span>Assigned To</span><input id="alert-reminder-assigned" value="${escapeHtml(state.actor)}" placeholder="jarrett" /></label>
          <label><span>Due At</span><input id="alert-reminder-due" type="datetime-local" /></label>
          <label><span>Reminder Note</span><textarea id="alert-reminder-note" placeholder="Follow up with customer"></textarea></label>
          <button class="button button-primary" id="alert-reminder-button">Create Reminder</button>
        </div>
      </section>
      <section class="detail-card">
        <h3>Metadata</h3>
        <pre>${escapeHtml(JSON.stringify(item.metadata_json || {}, null, 2))}</pre>
      </section>
      <section class="detail-card">
        <h3>Event History</h3>
        <div class="event-list">
          ${detail.events.map((event) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(event.event_type)}</strong><span class="dense">${formatDateTime(event.event_ts)}</span></div><div class="muted">${escapeHtml(event.actor || "system")}</div></div>`).join("")}
        </div>
      </section>
    </div>
  `;

  els.detailPanel.querySelector('[data-alert-action="ack"]').onclick = () => mutate(async () => {
    await api(`/api/alerts/${item.id}/ack?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadAlerts(true);
  }, "Alert acknowledged.");

  els.detailPanel.querySelector('[data-alert-action="resolve"]').onclick = () => mutate(async () => {
    await api(`/api/alerts/${item.id}/resolve?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadAlerts(true);
  }, "Alert resolved.");

  document.getElementById("alert-snooze-button").onclick = () => mutate(async () => {
    const snoozedUntil = toUtcIso(document.getElementById("alert-snooze-until").value);
    const note = document.getElementById("alert-snooze-note").value.trim();
    await api(`/api/alerts/${item.id}/snooze?actor=${encodeURIComponent(state.actor || "ui")}&snoozed_until=${encodeURIComponent(snoozedUntil)}&note=${encodeURIComponent(note)}`, { method: "POST", auth: true });
    await loadAlerts(true);
  }, "Alert snoozed.");

  document.getElementById("alert-reminder-button").onclick = () => mutate(async () => {
    const assignedTo = document.getElementById("alert-reminder-assigned").value.trim();
    const dueAt = toUtcIso(document.getElementById("alert-reminder-due").value);
    const note = document.getElementById("alert-reminder-note").value.trim();
    await api(`/api/alerts/${item.id}/reminder?actor=${encodeURIComponent(state.actor || "ui")}&assigned_to=${encodeURIComponent(assignedTo)}&due_at=${encodeURIComponent(dueAt)}&note=${encodeURIComponent(note)}`, { method: "POST", auth: true });
    showToast("Reminder created from alert.");
  }, "Reminder created.");
}

async function loadCustomers() {
  const result = await api(`/api/customers${qs(filters.customers)}`);
  state.data.customers = result;
  if (!state.selections.customerId && result.items[0]) state.selections.customerId = result.items[0].id;
  renderCustomers();
  if (state.selections.customerId) await loadCustomerDetail(state.selections.customerId);
}

function renderCustomers() {
  const result = state.data.customers;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Customer List</h3>
      <p class="panel-subtitle">${escapeHtml(result.total)} customers in scope.</p>
      <div class="item-list">
        ${result.items.map((item) => {
          const name = safeName(`${item.first_name || ""} ${item.last_name || ""}`.trim(), item.company_name, item.email, `Customer ${item.id}`);
          return `
            <article class="item-card ${state.selections.customerId === item.id ? "is-selected" : ""}" data-customer-id="${item.id}">
              <div class="item-card-header">
                <div>
                  <h4>${escapeHtml(name)}</h4>
                  <div class="muted">${escapeHtml(item.city || "")}${item.city && item.state ? ", " : ""}${escapeHtml(item.state || "")}</div>
                </div>
                <div class="meta-stack">${badge(item.customer_status || "unknown")} ${badge(item.is_operationally_active ? "open" : "cleared", item.is_operationally_active ? "open" : "cleared")}</div>
              </div>
              <div class="meta-row"><span>${escapeHtml(item.pool_count || 0)} pools · ${escapeHtml(item.open_alert_count || 0)} tracked alerts</span><span>${escapeHtml(item.mobile_phone || item.phone || "—")}</span></div>
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
  els.mainPanel.querySelectorAll("[data-customer-id]").forEach((el) => {
    el.onclick = async () => {
      state.selections.customerId = Number(el.dataset.customerId);
      renderCustomers();
      await loadCustomerDetail(state.selections.customerId);
    };
  });
}

async function loadCustomerDetail(customerId) {
  const detail = await api(`/api/customers/${customerId}`);
  const item = detail.item;
  const name = safeName(`${item.first_name || ""} ${item.last_name || ""}`.trim(), item.company_name, item.email, `Customer ${item.id}`);
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>${escapeHtml(name)}</h3>
        <div class="meta-stack">
          <div class="meta-row"><span>Status</span><strong>${escapeHtml(item.customer_status || "—")}</strong></div>
          <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
          <div class="meta-row"><span>Phone</span><strong>${escapeHtml(item.mobile_phone || item.phone || "—")}</strong></div>
          <div class="meta-row"><span>Latest Chemistry Service</span><strong>${formatDateTime(detail.latest_chemistry_service_date)}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Pools</h3>
        <div class="event-list">${detail.pools.map((pool) => `<div class="item-card"><strong>${escapeHtml(pool.name || `Pool ${pool.id}`)}</strong><div class="muted">${escapeHtml(pool.city || "")}${pool.city && pool.state ? ", " : ""}${escapeHtml(pool.state || "")}</div></div>`).join("") || `<div class="empty-state">No pools on file.</div>`}</div>
      </section>
      <section class="detail-card">
        <h3>Alerts</h3>
        <div class="event-list">${detail.alerts.slice(0, 8).map((alert) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(alert.title)}</strong>${badge(alert.status)}</div><div class="muted">${escapeHtml(alert.summary || "")}</div></div>`).join("") || `<div class="empty-state">No tracked alerts.</div>`}</div>
      </section>
      <section class="detail-card">
        <h3>Reminders</h3>
        <div class="event-list">${(detail.reminders || []).slice(0, 8).map((reminder) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(reminder.title)}</strong>${badge(reminder.status)}</div><div class="muted">${reminder.due_at ? `Due ${formatDateTime(reminder.due_at)}` : "No due date"}</div></div>`).join("") || `<div class="empty-state">No reminders for this customer.</div>`}</div>
      </section>
    </div>
  `;
}

async function loadTechnicians() {
  const result = await api(`/api/technicians${qs(filters.technicians)}`);
  state.data.technicians = result;
  if (!state.selections.techId && result.items[0]) state.selections.techId = result.items[0].tech_id;
  renderTechnicians();
  if (state.selections.techId) await loadTechnicianDetail(state.selections.techId);
}

function renderTechnicians() {
  const result = state.data.technicians;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Technician Coverage</h3>
      <p class="panel-subtitle">${escapeHtml(result.total)} operators in this filter set. Current assignment view is the default.</p>
      <div class="stat-grid">
        ${Object.entries(result.summary || {}).map(([key, value]) => `<article class="stat-card"><span class="muted">${escapeHtml(key.replaceAll("_", " "))}</span><strong>${escapeHtml(value)}</strong></article>`).join("")}
      </div>
    </section>
    <section class="section-card">
      <div class="item-list">
        ${result.items.map((item) => `
          <article class="item-card ${state.selections.techId === item.tech_id ? "is-selected" : ""}" data-tech-id="${escapeHtml(item.tech_id)}">
            <div class="item-card-header">
              <div>
                <h4>${escapeHtml(item.tech_name)}</h4>
                <div class="muted">${escapeHtml(item.role_type || "Unknown role")} · ${escapeHtml(item.username || item.tech_id)}</div>
              </div>
              <div class="meta-stack">${badge(item.is_active ? "acknowledged" : "cleared", item.is_active ? "acknowledged" : "cleared")} ${item.has_current_assignments ? badge("current", "info") : ""}</div>
            </div>
            <div class="meta-row"><span>${escapeHtml(item.customer_count)} customers · ${escapeHtml(item.route_stop_count_30d)} route stops / 30d</span><span>${formatDateTime(item.latest_service_date)}</span></div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  els.mainPanel.querySelectorAll("[data-tech-id]").forEach((el) => {
    el.onclick = async () => {
      state.selections.techId = el.dataset.techId;
      renderTechnicians();
      await loadTechnicianDetail(state.selections.techId);
    };
  });
}

async function loadTechnicianDetail(techId) {
  const detail = await api(`/api/technicians/${encodeURIComponent(techId)}`);
  const item = detail.item;
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>${escapeHtml(item.tech_name)}</h3>
        <div class="meta-stack">
          <div class="meta-row"><span>Role</span><strong>${escapeHtml(item.role_type || "—")}</strong></div>
          <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
          <div class="meta-row"><span>Current Assignments</span><strong>${escapeHtml(item.service_location_count)}</strong></div>
          <div class="meta-row"><span>Recent Route Activity</span><strong>${escapeHtml(item.route_stop_count_30d)}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Assigned Customers</h3>
        <div class="event-list">${detail.customers.map((customer) => `<div class="item-card"><strong>${escapeHtml(customer.customer_name || customer.source_customer_id)}</strong><div class="muted">${escapeHtml(customer.customer_status || "—")}</div></div>`).join("") || `<div class="empty-state">No current customer assignments.</div>`}</div>
      </section>
      <section class="detail-card">
        <h3>Current Locations</h3>
        <div class="event-list">${detail.service_locations.map((location) => `<div class="item-card"><strong>${escapeHtml(location.address || location.source_location_id || "Location")}</strong><div class="muted">${escapeHtml(location.city || "")}${location.city && location.state ? ", " : ""}${escapeHtml(location.state || "")}</div></div>`).join("") || `<div class="empty-state">No current locations.</div>`}</div>
      </section>
    </div>
  `;
}

async function loadReminders() {
  const result = await api(`/api/reminders${qs(filters.reminders)}`);
  state.data.reminders = result;
  if (!state.selections.reminderId && result.items[0]) state.selections.reminderId = result.items[0].id;
  renderReminders();
  if (state.selections.reminderId) await loadReminderDetail(state.selections.reminderId);
}

function renderReminders() {
  const result = state.data.reminders;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Reminder Queue</h3>
      <div class="stat-grid">
        ${Object.entries(result.summary || {}).map(([key, value]) => `<article class="stat-card"><span class="muted">${escapeHtml(key.replaceAll("_", " "))}</span><strong>${escapeHtml(value)}</strong></article>`).join("")}
      </div>
    </section>
    <section class="section-card">
      <div class="item-list">
        ${result.items.map((item) => `
          <article class="item-card ${state.selections.reminderId === item.id ? "is-selected" : ""}" data-reminder-id="${item.id}">
            <div class="item-card-header">
              <div>
                <h4>${escapeHtml(item.title)}</h4>
                <div class="muted">${escapeHtml(item.customer_name || "No customer")} · ${escapeHtml(item.pool_name || "No pool")}</div>
              </div>
              <div class="meta-stack">${badge(item.status)} ${badge(item.priority)}</div>
            </div>
            <div class="meta-row"><span>${escapeHtml(item.assigned_to || "Unassigned")}</span><span>${item.due_at ? `Due ${formatDateTime(item.due_at)}` : "No due date"}</span></div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  els.mainPanel.querySelectorAll("[data-reminder-id]").forEach((el) => {
    el.onclick = async () => {
      state.selections.reminderId = Number(el.dataset.reminderId);
      renderReminders();
      await loadReminderDetail(state.selections.reminderId);
    };
  });
}

async function loadReminderDetail(reminderId) {
  const detail = await api(`/api/reminders/${reminderId}`);
  const item = detail.item;
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-header">
          <div>
            <h3>${escapeHtml(item.title)}</h3>
            <p class="muted">${escapeHtml(item.summary || "")}</p>
          </div>
          <div class="meta-stack">${badge(item.status)} ${badge(item.priority)}</div>
        </div>
        <div class="meta-stack">
          <div class="meta-row"><span>Assigned To</span><strong>${escapeHtml(item.assigned_to || "—")}</strong></div>
          <div class="meta-row"><span>Due</span><strong>${formatDateTime(item.due_at)}</strong></div>
          <div class="meta-row"><span>Snoozed Until</span><strong>${formatDateTime(item.snoozed_until)}</strong></div>
          <div class="meta-row"><span>Linked Alert</span><strong>${escapeHtml(item.source_alert_title || item.source_alert_instance_id || "—")}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Reminder Actions</h3>
        <div class="detail-actions">
          <button class="button button-secondary" data-reminder-action="ack">Acknowledge</button>
          <button class="button button-primary" data-reminder-action="complete">Complete</button>
          <button class="button button-danger" data-reminder-action="cancel">Cancel</button>
        </div>
      </section>
      <section class="detail-card">
        <h3>Edit Reminder</h3>
        <div class="split">
          <label><span>Assigned To</span><input id="reminder-assigned" value="${escapeHtml(item.assigned_to || "")}" /></label>
          <label><span>Due At</span><input id="reminder-due" type="datetime-local" value="${toDatetimeLocalValue(item.due_at)}" /></label>
          <label><span>Title</span><input id="reminder-title" value="${escapeHtml(item.title || "")}" /></label>
          <label><span>Summary</span><textarea id="reminder-note">${escapeHtml(item.summary || "")}</textarea></label>
          <button class="button button-secondary" id="reminder-update-button">Save Reminder Changes</button>
        </div>
      </section>
      <section class="detail-card">
        <h3>Snooze Reminder</h3>
        <div class="split">
          <label><span>Snoozed Until</span><input id="reminder-snooze-until" type="datetime-local" value="${toDatetimeLocalValue(item.snoozed_until)}" /></label>
          <label><span>Snooze Note</span><textarea id="reminder-snooze-note" placeholder="Waiting on customer or route timing"></textarea></label>
          <button class="button button-secondary" id="reminder-snooze-button">Snooze Reminder</button>
        </div>
      </section>
      <section class="detail-card">
        <h3>Event History</h3>
        <div class="event-list">
          ${detail.events.map((event) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(event.event_type)}</strong><span class="dense">${formatDateTime(event.event_ts)}</span></div><div class="muted">${escapeHtml(event.actor || "system")}</div></div>`).join("")}
        </div>
      </section>
    </div>
  `;

  els.detailPanel.querySelector('[data-reminder-action="ack"]').onclick = () => mutate(async () => {
    await api(`/api/reminders/${item.id}/ack?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadReminders(true);
  }, "Reminder acknowledged.");

  els.detailPanel.querySelector('[data-reminder-action="complete"]').onclick = () => mutate(async () => {
    await api(`/api/reminders/${item.id}/complete?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadReminders(true);
  }, "Reminder completed.");

  els.detailPanel.querySelector('[data-reminder-action="cancel"]').onclick = () => mutate(async () => {
    await api(`/api/reminders/${item.id}/cancel?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadReminders(true);
  }, "Reminder canceled.");

  document.getElementById("reminder-update-button").onclick = () => mutate(async () => {
    const params = new URLSearchParams({
      actor: state.actor || "ui",
      assigned_to: document.getElementById("reminder-assigned").value.trim(),
      due_at: toUtcIso(document.getElementById("reminder-due").value),
      title: document.getElementById("reminder-title").value.trim(),
      note: document.getElementById("reminder-note").value.trim(),
    });
    await api(`/api/reminders/${item.id}/update?${params.toString()}`, { method: "POST", auth: true });
    await loadReminders(true);
  }, "Reminder updated.");

  document.getElementById("reminder-snooze-button").onclick = () => mutate(async () => {
    const params = new URLSearchParams({
      actor: state.actor || "ui",
      snoozed_until: toUtcIso(document.getElementById("reminder-snooze-until").value),
      note: document.getElementById("reminder-snooze-note").value.trim(),
    });
    await api(`/api/reminders/${item.id}/snooze?${params.toString()}`, { method: "POST", auth: true });
    await loadReminders(true);
  }, "Reminder snoozed.");
}

async function mutate(run, successMessage) {
  try {
    setStatus("Saving…", "warning");
    await run();
    setStatus("Live", "info");
    if (successMessage) showToast(successMessage);
  } catch (error) {
    setStatus("Error", "critical");
    showToast(error.message, 4400);
  }
}

function itemCard(title, meta, footer) {
  return `<article class="item-card"><div class="item-card-header"><strong>${escapeHtml(title)}</strong><div>${meta}</div></div><div class="muted">${footer}</div></article>`;
}

init();
