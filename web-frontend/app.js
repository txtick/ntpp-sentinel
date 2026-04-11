const DEFAULT_CUSTOMER_CHART_POLICY = {
  default_days: 90,
  range_days: [30, 90, 180, 365],
  hidden_metrics: ["free_chlorine", "combined_chlorine"],
  sparse_metrics: [],
  required_every_visit_metrics: [
    "total_chlorine",
    "ph",
    "temperature",
    "tds",
    "alkalinity",
    "lsi",
    "salt",
    "filter_pressure",
  ],
  monthly_metrics: [
    "phosphates",
    "calcium_hardness",
    "cya",
  ],
  chart_order: [
    "total_chlorine",
    "ph",
    "filter_pressure",
    "temperature",
    "alkalinity",
    "lsi",
    "cya",
    "calcium_hardness",
    "phosphates",
    "salt",
    "tds",
  ],
  recommended_highs: {
    total_chlorine: 5,
    ph: 9.36,
    temperature: 98.4,
    tds: 2400,
    alkalinity: 144,
    lsi: 0.36,
    salt: 4080,
    filter_pressure: 20,
    phosphates: 600,
    calcium_hardness: 480,
    cya: 60,
  },
  display_precision: {
    lsi: 2,
  },
  metric_labels: {
    ph: "pH",
    total_chlorine: "Total Chlorine",
    combined_chlorine: "Combined Chlorine",
    cya: "CYA",
    alkalinity: "Alkalinity",
    calcium_hardness: "Calcium Hardness",
    filter_pressure: "Filter Pressure",
    salt: "Salt",
    phosphates: "Phosphates",
    temperature: "Temperature",
    tds: "TDS",
    lsi: "LSI",
  },
};

const state = {
  view: "home",
  actor: localStorage.getItem("ntpp.actor") || "",
  secret: localStorage.getItem("ntpp.secret") || "",
  selections: {
    alertId: null,
    customerId: null,
    customerChartDays: DEFAULT_CUSTOMER_CHART_POLICY.default_days,
    customerVisitsExpanded: false,
    techId: null,
    reminderId: null,
  },
  config: {
    customerCharts: DEFAULT_CUSTOMER_CHART_POLICY,
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
  contentGrid: document.querySelector(".content-grid"),
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
  "alert-profile": { kicker: "Alert", title: "Alert Detail" },
  customers: { kicker: "Customers", title: "Customer Operations View" },
  "customer-profile": { kicker: "Customer", title: "Customer Chemistry Profile" },
  technicians: { kicker: "Technicians", title: "Field Operator Snapshot" },
  "technician-profile": { kicker: "Technician", title: "Technician Profile" },
  reminders: { kicker: "Reminders", title: "Follow-Up Queue" },
};

const filters = {
  alerts: { status: "", category: "", rule_code: "", search: "", limit: 40 },
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
    const activeView = ["customer-profile"].includes(view)
      ? "customers"
      : ["alert-profile"].includes(view)
        ? "alerts"
        : ["technician-profile"].includes(view)
          ? "technicians"
          : view;
    button.classList.toggle("is-active", button.dataset.view === activeView);
  });
  const meta = viewMeta[view];
  els.viewKicker.textContent = meta.kicker;
  els.viewTitle.textContent = meta.title;
  window.scrollTo({ top: 0, behavior: "auto" });
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

function setLayout(mode = "split") {
  const single = mode === "single";
  els.contentGrid.classList.toggle("is-single", single);
  els.detailPanel.style.display = single ? "none" : "block";
  els.mainPanel.style.gridColumn = single ? "1 / -1" : "";
}

function currency(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return new Intl.NumberFormat([], {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

function mergeCustomerChartPolicy(policy = {}) {
  return {
    ...DEFAULT_CUSTOMER_CHART_POLICY,
    ...policy,
    range_days: Array.isArray(policy.range_days) && policy.range_days.length
      ? policy.range_days
      : DEFAULT_CUSTOMER_CHART_POLICY.range_days,
    hidden_metrics: Array.isArray(policy.hidden_metrics)
      ? policy.hidden_metrics
      : DEFAULT_CUSTOMER_CHART_POLICY.hidden_metrics,
    sparse_metrics: Array.isArray(policy.sparse_metrics)
      ? policy.sparse_metrics
      : DEFAULT_CUSTOMER_CHART_POLICY.sparse_metrics,
    required_every_visit_metrics: Array.isArray(policy.required_every_visit_metrics)
      ? policy.required_every_visit_metrics
      : DEFAULT_CUSTOMER_CHART_POLICY.required_every_visit_metrics,
    monthly_metrics: Array.isArray(policy.monthly_metrics)
      ? policy.monthly_metrics
      : DEFAULT_CUSTOMER_CHART_POLICY.monthly_metrics,
    chart_order: Array.isArray(policy.chart_order)
      ? policy.chart_order
      : DEFAULT_CUSTOMER_CHART_POLICY.chart_order,
    recommended_highs: {
      ...DEFAULT_CUSTOMER_CHART_POLICY.recommended_highs,
      ...(policy.recommended_highs || {}),
    },
    display_precision: {
      ...DEFAULT_CUSTOMER_CHART_POLICY.display_precision,
      ...(policy.display_precision || {}),
    },
    metric_labels: {
      ...DEFAULT_CUSTOMER_CHART_POLICY.metric_labels,
      ...(policy.metric_labels || {}),
    },
  };
}

function customerChartPolicy() {
  return state.config.customerCharts || DEFAULT_CUSTOMER_CHART_POLICY;
}

function normalizeMetricKey(seriesItemOrKey, description = "") {
  const rawValue = typeof seriesItemOrKey === "string"
    ? seriesItemOrKey
    : seriesItemOrKey?.readingKey || seriesItemOrKey?.description || "value";
  const raw = String(rawValue || "").trim().toLowerCase().replace(/[\s_]+/g, "");
  const desc = String(
    typeof seriesItemOrKey === "string" ? description : seriesItemOrKey?.description || description || ""
  ).trim().toLowerCase();

  if ((raw === "none" || raw === "value") && (desc.includes("water temp") || desc.includes("water temperature"))) {
    return "temperature";
  }
  if ((raw === "none" || raw === "value") && desc.includes("filter pressure")) return "filter_pressure";
  if ((raw === "none" || raw === "value") && desc.includes("alkalinity")) return "alkalinity";
  if ((raw === "none" || raw === "value") && desc.includes("cyanuric")) return "cya";
  if ((raw === "none" || raw === "value") && desc.includes("phosphate")) return "phosphates";
  if ((raw === "none" || raw === "value") && desc.includes("salt")) return "salt";
  if ((raw === "none" || raw === "value") && desc.includes("tds")) return "tds";
  if ((raw === "none" || raw === "value") && (desc.includes("saturation index") || desc.includes("(lsi)") || desc === "lsi")) return "lsi";
  if ((raw === "none" || raw === "value") && desc.includes("hardness")) return "calcium_hardness";
  if ((raw === "none" || raw === "value") && desc.includes("chlorine")) return "total_chlorine";
  if ((raw === "none" || raw === "value") && desc === "ph") return "ph";
  if (raw === "freechlorine") return "free_chlorine";
  if (raw === "totalchlorine") return "total_chlorine";
  if (raw === "combinedchlorine") return "combined_chlorine";
  if (raw === "cyanuricacid" || raw === "cya") return "cya";
  if (raw === "lsi" || raw === "saturationindex" || raw === "saturationindex(lsi)") return "lsi";
  if (raw === "totalalkalinity" || raw === "alkalinity") return "alkalinity";
  if (raw === "totalhardness" || raw === "calciumhardness") return "calcium_hardness";
  if (raw === "watertemperature" || raw === "temperature") return "temperature";
  if (raw === "tds") return "tds";
  if (raw === "none" && desc.includes("filter pressure")) return "filter_pressure";
  if (raw === "filterpressure" || raw === "psi") return "filter_pressure";
  return String(rawValue || "").trim().toLowerCase().replace(/\s+/g, "_");
}

function formatMetricLabel(seriesItem) {
  const raw = normalizeMetricKey(seriesItem);
  const labels = customerChartPolicy().metric_labels || {};
  if (labels[raw]) return labels[raw];
  return String(raw)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatShortDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString([], {
    month: "short",
    day: "numeric",
  });
}

function formatAxisValue(value, readingKey = "") {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const normalizedKey = normalizeMetricKey(readingKey);
  const precision = customerChartPolicy().display_precision?.[normalizedKey];
  if (Number.isFinite(Number(precision))) return number.toFixed(Number(precision));
  return number.toFixed(0);
}

function chemistrySeriesMeta(seriesItem) {
  const readingKey = normalizeMetricKey(seriesItem);
  const policy = customerChartPolicy();
  return {
    hide: (policy.hidden_metrics || []).includes(readingKey),
    unitLabel: "",
    sparse: isSparseChecklistMetric(readingKey),
  };
}

function isSparseChecklistMetric(readingKey) {
  return new Set(customerChartPolicy().sparse_metrics || []).has(normalizeMetricKey(readingKey));
}

function isLikelyUntestedPoint(row) {
  const readingKey = normalizeMetricKey(row.reading_key, row.description);
  const value = Number(row.value);
  if (!isSparseChecklistMetric(readingKey) || value !== 0) return false;
  const selectedIndex = Number(
    row?.raw_json?.service_stop_entry?.SelectedIndex ??
    row?.raw_json?.service_stop_entry?.selected_index
  );
  if (Number.isFinite(selectedIndex)) return selectedIndex === 0;
  return true;
}

function filterSeriesByDays(series, days) {
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  return series
    .map((seriesItem) => ({
      ...seriesItem,
      points: (seriesItem.points || []).filter((point) => {
        const ts = new Date(point.service_date).getTime();
        return Number.isFinite(ts) && ts >= cutoff;
      }),
    }))
    .filter((seriesItem) => seriesItem.points.length > 0);
}

function alertReasonBadge(item) {
  const metadata = item.metadata_json || {};
  if (item.rule_code === "filter_clean_missing_psi") {
    const windowDays = metadata.window_days || 90;
    return badge(`No PSI in ${windowDays}d`, "info");
  }
  return "";
}

function formatAlertSummary(item) {
  const metadata = item.metadata_json || {};
  if (item.category === "revenue" && metadata.opportunity_type === "chemical_cost_review") {
    const observedCost = currency(metadata.observed_count);
    if (observedCost) {
      return `Chemical spend flagged at ${observedCost} in the review window.`;
    }
  }
  return item.summary || "No summary available.";
}

function formatAlertSubline(item) {
  const metadata = item.metadata_json || {};
  const assignedTech = metadata.assigned_technician?.tech_name;
  if (item.category === "revenue" && metadata.opportunity_type === "chemical_cost_review") {
    const observedCost = currency(metadata.observed_count);
    const threshold = currency(metadata.threshold_value);
    if (observedCost && threshold) {
      return `revenue · cost ${observedCost} vs threshold ${threshold}${assignedTech ? ` · tech ${assignedTech}` : ""}`;
    }
    if (observedCost) {
      return `revenue · cost ${observedCost}${assignedTech ? ` · tech ${assignedTech}` : ""}`;
    }
  }
  return `${item.category} · rule ${item.rule_code}${assignedTech ? ` · tech ${assignedTech}` : ""}`;
}

function badge(value, typeHint = "") {
  const normalized = String(typeHint || value || "muted").toLowerCase().replace(/\s+/g, "-");
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

function wireNavigationTargets(root = document) {
  root.querySelectorAll("[data-customer-id]").forEach((el) => {
    el.onclick = () => {
      state.selections.customerId = Number(el.dataset.customerId);
      state.selections.customerChartDays = customerChartPolicy().default_days || DEFAULT_CUSTOMER_CHART_POLICY.default_days;
      state.selections.customerVisitsExpanded = false;
      setView("customer-profile");
    };
  });
  root.querySelectorAll("[data-alert-id]").forEach((el) => {
    el.onclick = () => {
      state.selections.alertId = Number(el.dataset.alertId);
      setView("alert-profile");
    };
  });
  root.querySelectorAll("[data-tech-id]").forEach((el) => {
    el.onclick = () => {
      state.selections.techId = el.dataset.techId;
      setView("technician-profile");
    };
  });
  root.querySelectorAll("[data-reminder-id]").forEach((el) => {
    el.onclick = async () => {
      state.selections.reminderId = Number(el.dataset.reminderId);
      if (state.view === "reminders") {
        renderReminders();
        await loadReminderDetail(state.selections.reminderId);
        return;
      }
      setView("reminders");
    };
  });
}

function renderFilters() {
  if (state.view === "home") {
    setLayout("split");
    els.filters.innerHTML = `<div class="muted">Live snapshot of backend summary, recent alerts, and reminder pressure.</div>`;
    return;
  }

  if (state.view === "alerts") {
    setLayout("split");
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
      <label class="filter-chip"><span>Revenue Rule</span>
        <select id="filter-alert-rule">
          <option value="">All</option>
          <option value="chemical_cost_review_high" ${f.rule_code === "chemical_cost_review_high" ? "selected" : ""}>Chemical Cost Review</option>
          <option value="filter_clean_trend" ${f.rule_code === "filter_clean_trend" ? "selected" : ""}>Filter Clean Trend</option>
          <option value="filter_clean_missing_psi" ${f.rule_code === "filter_clean_missing_psi" ? "selected" : ""}>Filter Clean Missing PSI</option>
          <option value="drain_refill_cya_repeat" ${f.rule_code === "drain_refill_cya_repeat" ? "selected" : ""}>Drain / Refill</option>
          <option value="phosphate_treatment_high" ${f.rule_code === "phosphate_treatment_high" ? "selected" : ""}>Phosphate Treatment</option>
        </select>
      </label>
      <label class="filter-chip"><span>Search</span><input id="filter-alert-search" value="${escapeHtml(f.search)}" placeholder="Customer, rule, pool, opportunity" /></label>
    `;
    document.getElementById("filter-alert-status").onchange = (e) => {
      filters.alerts.status = e.target.value;
      loadAlerts(true);
    };
    document.getElementById("filter-alert-category").onchange = (e) => {
      filters.alerts.category = e.target.value;
      loadAlerts(true);
    };
    document.getElementById("filter-alert-rule").onchange = (e) => {
      filters.alerts.rule_code = e.target.value;
      loadAlerts(true);
    };
    document.getElementById("filter-alert-search").onchange = (e) => {
      filters.alerts.search = e.target.value.trim();
      loadAlerts(true);
    };
    return;
  }

  if (state.view === "customers") {
    setLayout("split");
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
    setLayout("split");
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
    setLayout("split");
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
    return;
  }

  if (state.view === "customer-profile") {
    setLayout("single");
    const customerId = state.selections.customerId;
    els.filters.innerHTML = `
      <div class="header-actions">
        <button id="customer-profile-back" class="button button-secondary">Back To Customers</button>
        <div class="muted">${customerId ? `Customer ID ${escapeHtml(customerId)}` : "No customer selected"}</div>
      </div>
    `;
    document.getElementById("customer-profile-back").onclick = () => setView("customers");
    return;
  }

  if (state.view === "alert-profile") {
    setLayout("single");
    const alertId = state.selections.alertId;
    els.filters.innerHTML = `
      <div class="header-actions">
        <button id="alert-profile-back" class="button button-secondary">Back To Alerts</button>
        <div class="muted">${alertId ? `Alert ID ${escapeHtml(alertId)}` : "No alert selected"}</div>
      </div>
    `;
    document.getElementById("alert-profile-back").onclick = () => setView("alerts");
    return;
  }

  if (state.view === "technician-profile") {
    setLayout("single");
    const techId = state.selections.techId;
    els.filters.innerHTML = `
      <div class="header-actions">
        <button id="technician-profile-back" class="button button-secondary">Back To Technicians</button>
        <div class="muted">${techId ? `Technician ID ${escapeHtml(techId)}` : "No technician selected"}</div>
      </div>
    `;
    document.getElementById("technician-profile-back").onclick = () => setView("technicians");
  }
}

async function loadCurrentView(force = false) {
  try {
    setStatus("Loading…", "warning");
    if (state.view === "home") await loadHome(force);
    if (state.view === "alerts") await loadAlerts(force);
    if (state.view === "alert-profile") await loadAlertProfile(force);
    if (state.view === "customers") await loadCustomers(force);
    if (state.view === "customer-profile") await loadCustomerProfile(force);
    if (state.view === "technicians") await loadTechnicians(force);
    if (state.view === "technician-profile") await loadTechnicianProfile(force);
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
        ${state.data.home.alerts.items.map((item) => `<article class="item-card is-clickable" data-alert-id="${escapeHtml(item.id)}"><div class="item-card-header"><strong>${escapeHtml(item.title)}</strong><div>${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}</div></div><div class="muted">${formatDateTime(item.last_detected_at)}</div></article>`).join("")}
      </div>
    </section>
    <section class="section-card">
      <h3>Reminder Pressure</h3>
      <div class="item-list">
        ${state.data.home.reminders.items.map((item) => `<article class="item-card is-clickable" data-reminder-id="${escapeHtml(item.id)}"><div class="item-card-header"><strong>${escapeHtml(item.title)}</strong><div>${badge(item.status)} ${item.assigned_to ? `<span class="dense">${escapeHtml(item.assigned_to)}</span>` : ""}</div></div><div class="muted">${item.due_at ? `Due ${formatDateTime(item.due_at)}` : "No due date"}</div></article>`).join("") || `<div class="empty-state">No reminders in queue.</div>`}
      </div>
    </section>
  `;
  wireNavigationTargets(els.mainPanel);

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
                <div class="muted">${escapeHtml(formatAlertSummary(item))}</div>
              </div>
              <div class="meta-stack">${alertReasonBadge(item)} ${badge(item.severity)} ${badge(item.status)}</div>
            </div>
            <div class="meta-row">
              <span>${escapeHtml(formatAlertSubline(item))}</span>
              <span>${formatDateTime(item.last_detected_at)}</span>
            </div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  wireNavigationTargets(els.mainPanel);
}

function weekdaySortValue(dayOfWeek) {
  const normalized = String(dayOfWeek || "").trim().toLowerCase();
  const order = {
    monday: 1,
    tuesday: 2,
    wednesday: 3,
    thursday: 4,
    friday: 5,
    saturday: 6,
    sunday: 7,
  };
  return order[normalized] || 8;
}

function groupAssignmentsByDay(assignments) {
  const groups = new Map();
  assignments.forEach((assignment) => {
    const dayLabel = assignment.day_of_week || "Unscheduled";
    if (!groups.has(dayLabel)) groups.set(dayLabel, []);
    groups.get(dayLabel).push(assignment);
  });
  return Array.from(groups.entries())
    .sort((a, b) => weekdaySortValue(a[0]) - weekdaySortValue(b[0]) || a[0].localeCompare(b[0]))
    .map(([dayLabel, items]) => ({ dayLabel, items }));
}

async function loadAlertDetail(alertId) {
  const detail = await api(`/api/alerts/${alertId}`);
  renderAlertDetail(detail);
}

async function loadAlertProfile() {
  const alertId = state.selections.alertId;
  if (!alertId) {
    els.mainPanel.innerHTML = `<div class="empty-state">Select an alert first.</div>`;
    return;
  }
  const detail = await api(`/api/alerts/${alertId}`);
  renderAlertProfile(detail);
}

function renderAlertDetail(detail) {
  const item = detail.item;
  const metadata = item.metadata_json || {};
  const observedCost = currency(metadata.observed_count);
  const thresholdCost = currency(metadata.threshold_value);
  const assignedTech = metadata.assigned_technician?.tech_name || "Unassigned";
  const recentTech = metadata.recent_service_technician?.tech_name || "No recent route stop";
  const visitBreakdown = Array.isArray(metadata.visit_breakdown) ? metadata.visit_breakdown : [];
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <div class="detail-header">
          <div>
            <h3>${escapeHtml(item.title)}</h3>
            <p class="muted">${escapeHtml(formatAlertSummary(item))}</p>
          </div>
          <div class="meta-stack">${alertReasonBadge(item)} ${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}</div>
        </div>
        <div class="meta-stack">
          <div class="meta-row"><span>Customer</span><strong>${escapeHtml(metadata.customer_name || item.customer_id || "—")}</strong></div>
          <div class="meta-row"><span>Pool</span><strong>${escapeHtml(metadata.pool_name || item.pool_id || "—")}</strong></div>
          <div class="meta-row"><span>Assigned Technician</span><strong>${escapeHtml(assignedTech)}</strong></div>
          <div class="meta-row"><span>Recent Service Tech</span><strong>${escapeHtml(recentTech)}</strong></div>
          ${observedCost ? `<div class="meta-row"><span>Observed Cost</span><strong>${escapeHtml(observedCost)}</strong></div>` : ""}
          ${thresholdCost ? `<div class="meta-row"><span>Threshold</span><strong>${escapeHtml(thresholdCost)}</strong></div>` : ""}
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
        <h3>Visit Breakdown</h3>
        <div class="event-list">
          ${visitBreakdown.length ? visitBreakdown.map((visit) => `
            <div class="item-card">
              <div class="item-card-header">
                <strong>${formatDateTime(visit.service_date)}</strong>
                <span class="dense">${escapeHtml(currency(visit.visit_estimated_cost) || String(visit.visit_estimated_cost || "—"))}</span>
              </div>
              <div class="muted">${escapeHtml(visit.technician_name || "No technician linked")}</div>
              <div class="chem-list">
                ${(visit.chemicals || []).map((chem) => `
                  <div class="chem-chip">
                    <span>${escapeHtml(chem.description || chem.dosage_key || "Chemical")}${chem.quantity != null ? ` · ${escapeHtml(String(chem.quantity))} ${escapeHtml(chem.unit_of_measure || "")}` : ""}</span>
                    <span class="dense">${escapeHtml(currency(chem.estimated_cost) || String(chem.estimated_cost || "—"))}</span>
                  </div>
                `).join("")}
              </div>
            </div>
          `).join("") : `<div class="empty-state">No visit-level chemical detail linked for this alert yet.</div>`}
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

function renderAlertProfile(detail) {
  const item = detail.item;
  const metadata = item.metadata_json || {};
  const observedCost = currency(metadata.observed_count);
  const thresholdCost = currency(metadata.threshold_value);
  const assignedTech = metadata.assigned_technician?.tech_name || "Unassigned";
  const recentTech = metadata.recent_service_technician?.tech_name || "No recent route stop";
  const visitBreakdown = Array.isArray(metadata.visit_breakdown) ? metadata.visit_breakdown : [];

  els.viewKicker.textContent = "Alert";
  els.viewTitle.textContent = item.title;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <div class="detail-header">
        <div>
          <h3>${escapeHtml(item.title)}</h3>
          <p class="panel-subtitle">${escapeHtml(formatAlertSummary(item))}</p>
        </div>
        <div class="meta-stack">${alertReasonBadge(item)} ${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}</div>
      </div>
      <div class="stat-grid">
        <article class="stat-card"><span class="muted">Customer</span><strong>${escapeHtml(metadata.customer_name || item.customer_id || "—")}</strong></article>
        <article class="stat-card"><span class="muted">Pool</span><strong>${escapeHtml(metadata.pool_name || item.pool_id || "—")}</strong></article>
        <article class="stat-card"><span class="muted">Assigned Tech</span><strong>${escapeHtml(assignedTech)}</strong></article>
        <article class="stat-card"><span class="muted">Recent Tech</span><strong>${escapeHtml(recentTech)}</strong></article>
      </div>
      <div class="meta-stack">
        ${observedCost ? `<div class="meta-row"><span>Observed Cost</span><strong>${escapeHtml(observedCost)}</strong></div>` : ""}
        ${thresholdCost ? `<div class="meta-row"><span>Threshold</span><strong>${escapeHtml(thresholdCost)}</strong></div>` : ""}
        <div class="meta-row"><span>Last Detected</span><strong>${formatDateTime(item.last_detected_at)}</strong></div>
        <div class="meta-row"><span>Snoozed Until</span><strong>${formatDateTime(item.snoozed_until)}</strong></div>
      </div>
    </section>
    <section class="section-card">
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
    <section class="section-card">
      <h3>Create Reminder</h3>
      <div class="split">
        <label><span>Assigned To</span><input id="alert-reminder-assigned" value="${escapeHtml(state.actor)}" placeholder="jarrett" /></label>
        <label><span>Due At</span><input id="alert-reminder-due" type="datetime-local" /></label>
        <label><span>Reminder Note</span><textarea id="alert-reminder-note" placeholder="Follow up with customer"></textarea></label>
        <button class="button button-primary" id="alert-reminder-button">Create Reminder</button>
      </div>
    </section>
    <section class="section-card">
      <h3>Visit Breakdown</h3>
      <div class="event-list">
        ${visitBreakdown.length ? visitBreakdown.map((visit) => `
          <div class="item-card">
            <div class="item-card-header">
              <strong>${formatDateTime(visit.service_date)}</strong>
              <span class="dense">${escapeHtml(currency(visit.visit_estimated_cost) || String(visit.visit_estimated_cost || "—"))}</span>
            </div>
            <div class="muted">${escapeHtml(visit.technician_name || "No technician linked")}</div>
            <div class="chem-list">
              ${(visit.chemicals || []).map((chem) => `
                <div class="chem-chip">
                  <span>${escapeHtml(chem.description || chem.dosage_key || "Chemical")}${chem.quantity != null ? ` · ${escapeHtml(String(chem.quantity))} ${escapeHtml(chem.unit_of_measure || "")}` : ""}</span>
                  <span class="dense">${escapeHtml(currency(chem.estimated_cost) || String(chem.estimated_cost || "—"))}</span>
                </div>
              `).join("")}
            </div>
          </div>
        `).join("") : `<div class="empty-state">No visit-level chemical detail linked for this alert yet.</div>`}
      </div>
    </section>
    <section class="section-card">
      <h3>Event History</h3>
      <div class="event-list">
        ${detail.events.map((event) => `<div class="item-card"><div class="item-card-header"><strong>${escapeHtml(event.event_type)}</strong><span class="dense">${formatDateTime(event.event_ts)}</span></div><div class="muted">${escapeHtml(event.actor || "system")}</div></div>`).join("")}
      </div>
    </section>
    <section class="section-card">
      <h3>Metadata</h3>
      <pre>${escapeHtml(JSON.stringify(item.metadata_json || {}, null, 2))}</pre>
    </section>
  `;

  els.mainPanel.querySelector('[data-alert-action="ack"]').onclick = () => mutate(async () => {
    await api(`/api/alerts/${item.id}/ack?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadAlertProfile(true);
  }, "Alert acknowledged.");

  els.mainPanel.querySelector('[data-alert-action="resolve"]').onclick = () => mutate(async () => {
    await api(`/api/alerts/${item.id}/resolve?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
    await loadAlertProfile(true);
  }, "Alert resolved.");

  document.getElementById("alert-snooze-button").onclick = () => mutate(async () => {
    const snoozedUntil = toUtcIso(document.getElementById("alert-snooze-until").value);
    const note = document.getElementById("alert-snooze-note").value.trim();
    await api(`/api/alerts/${item.id}/snooze?actor=${encodeURIComponent(state.actor || "ui")}&snoozed_until=${encodeURIComponent(snoozedUntil)}&note=${encodeURIComponent(note)}`, { method: "POST", auth: true });
    await loadAlertProfile(true);
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
      <p class="panel-subtitle">${escapeHtml(result.total)} customers in scope. Click any customer to open the full chemistry profile.</p>
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
  wireNavigationTargets(els.mainPanel);

  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>Customer Profiles</h3>
        <p class="muted">Each customer profile will show chemistry trend charts, chemical spend by visit, current alerts, and reminder context on a full-width page.</p>
      </section>
    </div>
  `;
}

async function loadCustomerDetail(customerId) {
  const detail = await api(`/api/customers/${customerId}`);
  const item = detail.item;
  const name = safeName(`${item.first_name || ""} ${item.last_name || ""}`.trim(), item.company_name, item.email, `Customer ${item.id}`);
  const poolsSummary = detail.pools.length
    ? detail.pools
        .map((pool) => {
          const location = [pool.city, pool.state].filter(Boolean).join(", ");
          return `${pool.name || `Pool ${pool.id}`}${location ? ` · ${location}` : ""}`;
        })
        .join(" | ")
    : "No pools on file.";
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>${escapeHtml(name)}</h3>
        <div class="meta-stack">
          <div class="meta-row"><span>Status</span><strong>${escapeHtml(item.customer_status || "—")}</strong></div>
          <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
          <div class="meta-row"><span>Phone</span><strong>${escapeHtml(item.mobile_phone || item.phone || "—")}</strong></div>
          <div class="meta-row"><span>Latest Chemistry Service</span><strong>${formatDateTime(detail.latest_chemistry_service_date)}</strong></div>
          <div class="meta-row"><span>Pools</span><strong>${escapeHtml(String(detail.pools.length || 0))}</strong></div>
        </div>
        <p class="muted">${escapeHtml(poolsSummary)}</p>
      </section>
      <section class="detail-card">
        <h3>Alerts</h3>
        <div class="event-list">${detail.alerts.slice(0, 8).map((alert) => `<div class="item-card is-clickable" data-alert-id="${escapeHtml(alert.id)}"><div class="item-card-header"><strong>${escapeHtml(alert.title)}</strong>${badge(alert.status)}</div><div class="muted">${escapeHtml(alert.summary || "")}</div></div>`).join("") || `<div class="empty-state">No tracked alerts.</div>`}</div>
      </section>
      <section class="detail-card">
        <h3>Reminders</h3>
        <div class="event-list">${(detail.reminders || []).slice(0, 8).map((reminder) => `<div class="item-card is-clickable" data-reminder-id="${escapeHtml(reminder.id)}"><div class="item-card-header"><strong>${escapeHtml(reminder.title)}</strong>${badge(reminder.status)}</div><div class="muted">${reminder.due_at ? `Due ${formatDateTime(reminder.due_at)}` : "No due date"}</div></div>`).join("") || `<div class="empty-state">No reminders for this customer.</div>`}</div>
      </section>
    </div>
  `;
  wireNavigationTargets(els.detailPanel);
}

function buildLineChart(seriesItem) {
  const points = seriesItem.points || [];
  if (!points.length) {
    return `<div class="empty-state">No chart data.</div>`;
  }
  const width = 420;
  const height = 220;
  const margin = { top: 20, right: 18, bottom: 42, left: 54 };
  const values = points.map((point) => Number(point.value)).filter((value) => Number.isFinite(value));
  if (!values.length) {
    return `<div class="empty-state">No numeric values to chart.</div>`;
  }
  const xMin = margin.left;
  const xMax = width - margin.right;
  const yMin = margin.top;
  const yMax = height - margin.bottom;
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const readingKey = normalizeMetricKey(seriesItem);
  const recommendedHigh = Number(customerChartPolicy().recommended_highs?.[readingKey]);
  const hasRecommendedHigh = Number.isFinite(recommendedHigh) && recommendedHigh > 0;
  const paddedMin = 0;
  const paddedMax = hasRecommendedHigh ? recommendedHigh : Math.max(rawMax * 1.2, 10);
  const valueRange = paddedMax - paddedMin || 1;
  const span = paddedMax - paddedMin;
  const stepX = points.length === 1 ? 0 : (xMax - xMin) / (points.length - 1);
  const yTicks = Array.from({ length: 5 }, (_, index) => paddedMax - (span * index) / 4);
  const xTickIndexes = Array.from(new Set([
    0,
    Math.max(0, Math.floor((points.length - 1) / 2)),
    Math.max(0, points.length - 1),
  ])).sort((a, b) => a - b);

  const xForIndex = (index) => (points.length === 1 ? (xMin + xMax) / 2 : xMin + stepX * index);
  const yForValue = (value) => yMax - ((Number(value) - paddedMin) / valueRange) * (yMax - yMin);

  const path = points.map((point, index) => {
    const x = xForIndex(index);
    const y = yForValue(point.value);
    return `${index === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");

  const dots = points.map((point, index) => {
    const x = xForIndex(index);
    const y = yForValue(point.value);
    const meta = chemistrySeriesMeta(seriesItem);
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${meta.sparse ? "4.6" : "3.5"}" class="chart-dot${meta.sparse ? " chart-dot-sparse" : ""}">
      <title>${escapeHtml(`${formatShortDate(point.service_date)}: ${formatAxisValue(point.value, readingKey)}`)}</title>
    </circle>`;
  }).join("");

  const metricLabel = formatMetricLabel(seriesItem);
  const meta = chemistrySeriesMeta(seriesItem);
  return `
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Chemistry trend chart">
      ${yTicks.map((tick) => {
        const y = yForValue(tick);
        return `
          <line x1="${xMin}" y1="${y.toFixed(1)}" x2="${xMax}" y2="${y.toFixed(1)}" class="chart-gridline" />
          <text x="${xMin - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="chart-tick-label">${escapeHtml(formatAxisValue(tick, readingKey))}</text>
        `;
      }).join("")}
      ${xTickIndexes.map((index) => {
        const x = xForIndex(index);
        return `
          <line x1="${x.toFixed(1)}" y1="${yMax}" x2="${x.toFixed(1)}" y2="${(yMax + 6).toFixed(1)}" class="chart-axis" />
          <text x="${x.toFixed(1)}" y="${height - 14}" text-anchor="middle" class="chart-tick-label">${escapeHtml(formatShortDate(points[index]?.service_date))}</text>
        `;
      }).join("")}
      <line x1="${xMin}" y1="${yMax}" x2="${xMax}" y2="${yMax}" class="chart-axis" />
      <line x1="${xMin}" y1="${yMin}" x2="${xMin}" y2="${yMax}" class="chart-axis" />
      <text x="${margin.left - 42}" y="${(height / 2)}" text-anchor="middle" transform="rotate(-90 ${margin.left - 42} ${height / 2})" class="chart-axis-label">${escapeHtml(metricLabel)}</text>
      <text x="${(width / 2)}" y="${height - 2}" text-anchor="middle" class="chart-axis-label">Service Date</text>
      <path d="${path}" fill="none" stroke="#1f6b72" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
      ${dots}
    </svg>
  `;
}

function groupChemistrySeries(rows) {
  const groups = new Map();
  rows.forEach((row) => {
    const normalizedReadingKey = normalizeMetricKey({ readingKey: row.reading_key, description: row.description });
    if (chemistrySeriesMeta({ readingKey: normalizedReadingKey, description: row.description }).hide) return;
    const key = `${row.pool_id}::${normalizedReadingKey}`;
    if (!groups.has(key)) {
      groups.set(key, {
        poolId: row.pool_id,
        poolName: row.pool_name || `Pool ${row.pool_id}`,
        readingKey: normalizedReadingKey,
        readingType: row.reading_type,
        description: row.description,
        unitOfMeasure: row.unit_of_measure,
        points: [],
      });
    }
    groups.get(key).points.push(row);
  });
  const order = customerChartPolicy().chart_order || [];
  const rank = (key) => {
    const index = order.indexOf(normalizeMetricKey(key));
    return index === -1 ? Number.MAX_SAFE_INTEGER : index;
  };
  return Array.from(groups.values()).sort((a, b) => {
    const rankDiff = rank(a.readingKey) - rank(b.readingKey);
    if (rankDiff !== 0) return rankDiff;
    if (a.poolName !== b.poolName) return a.poolName.localeCompare(b.poolName);
    return String(a.readingKey).localeCompare(String(b.readingKey));
  });
}

async function loadCustomerProfile() {
  const customerId = state.selections.customerId;
  if (!customerId) {
    els.mainPanel.innerHTML = `<div class="empty-state">Select a customer first.</div>`;
    return;
  }
  const detail = await api(`/api/customers/${customerId}`);
  state.config.customerCharts = mergeCustomerChartPolicy(detail.chart_policy || {});
  const item = detail.item;
  const name = safeName(`${item.first_name || ""} ${item.last_name || ""}`.trim(), item.company_name, item.email, `Customer ${item.id}`);
  const policy = customerChartPolicy();
  const chartDays = Number(state.selections.customerChartDays || policy.default_days || DEFAULT_CUSTOMER_CHART_POLICY.default_days);
  const rangeDays = (policy.range_days || DEFAULT_CUSTOMER_CHART_POLICY.range_days).filter((days) => Number.isFinite(Number(days)) && Number(days) > 0);
  const series = filterSeriesByDays(groupChemistrySeries(detail.chemistry_history || []), chartDays);
  const multiplePools = (detail.pools || []).length > 1;
  const spend = detail.chemical_spend_summary || {};
  const visits = detail.chemical_spend_by_visit || [];
  const visits90d = visits.filter((visit) => {
    const ts = new Date(visit.service_date).getTime();
    return Number.isFinite(ts) && ts >= Date.now() - 90 * 24 * 60 * 60 * 1000;
  });
  const visibleVisits = state.selections.customerVisitsExpanded ? visits90d : visits90d.slice(0, 4);
  const visitCosts90d = visits90d
    .map((visit) => Number(visit.visit_estimated_cost))
    .filter((value) => Number.isFinite(value));
  const avgVisitCost90d = visitCosts90d.length
    ? visitCosts90d.reduce((sum, value) => sum + value, 0) / visitCosts90d.length
    : null;
  const latestVisitCost = visits.length ? Number(visits[0].visit_estimated_cost) : null;
  const assignedTechnicians = detail.assigned_technicians || [];
  const assignedTechLabel = assignedTechnicians.length
    ? assignedTechnicians.map((tech) => {
        const cadence = [tech.day_of_week, tech.frequency].filter(Boolean).join(" · ");
        return cadence ? `${tech.technician_name} (${cadence})` : tech.technician_name;
      }).join(", ")
    : "No current technician assignment";

  els.viewKicker.textContent = "Customer";
  els.viewTitle.textContent = `${name}`;

  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>${escapeHtml(name)}</h3>
      <p class="panel-subtitle">Chemistry trend view, reminders, spend, and tracked workflow context for this customer.</p>
      <div class="meta-stack">
        <div class="meta-row"><span>Current Assigned Tech</span><strong>${escapeHtml(assignedTechLabel)}</strong></div>
        <div class="meta-row"><span>Latest Chemistry Service</span><strong>${formatDateTime(detail.latest_chemistry_service_date)}</strong></div>
      </div>
    </section>
    <div class="profile-grid">
      <div class="profile-main">
        <section class="section-card">
          <div class="detail-header">
            <div>
              <h3>Chemistry Trend Charts</h3>
              <p class="panel-subtitle">Default view is 3 months. Use the range buttons to zoom in or out.</p>
            </div>
            <div class="range-controls">
              ${rangeDays.map((days) => `<button class="button button-secondary${chartDays === days ? " is-active-filter" : ""}" data-chart-range="${days}">${days >= 365 ? "12 Mo" : days >= 180 ? "6 Mo" : days >= 90 ? "3 Mo" : "30 D"}</button>`).join("")}
            </div>
          </div>
          <div class="chart-grid chart-grid-wide">
            ${series.length ? series.map((seriesItem) => {
              const meta = chemistrySeriesMeta(seriesItem);
              const latest = seriesItem.points[seriesItem.points.length - 1];
              const values = seriesItem.points.map((point) => Number(point.value)).filter((value) => Number.isFinite(value));
              if (meta.hide) return "";
              return `
                <article class="chart-card chart-card-large">
                  <div>
                    <h4>${escapeHtml(formatMetricLabel(seriesItem))}</h4>
                    ${multiplePools && seriesItem.poolName && seriesItem.poolName !== "Pool" ? `<div class="muted">${escapeHtml(seriesItem.poolName)}</div>` : ""}
                  </div>
                    ${buildLineChart(seriesItem)}
                  <div class="chart-caption">
                    <span>Latest ${escapeHtml(formatAxisValue(latest?.value, seriesItem.readingKey))}${meta.unitLabel ? ` ${escapeHtml(meta.unitLabel)}` : ""}</span>
                    <span>Min ${escapeHtml(formatAxisValue(values.length ? Math.min(...values) : "—", seriesItem.readingKey))} · Max ${escapeHtml(formatAxisValue(values.length ? Math.max(...values) : "—", seriesItem.readingKey))}</span>
                  </div>
                </article>
              `;
            }).join("") : `<div class="empty-state">No chemistry history available in this date range.</div>`}
          </div>
        </section>
        <section class="section-card">
          <h3>Reminders</h3>
          <div class="event-list">
            ${detail.reminders.length ? detail.reminders.map((reminder) => `<div class="item-card is-clickable" data-reminder-id="${escapeHtml(reminder.id)}"><div class="item-card-header"><strong>${escapeHtml(reminder.title)}</strong>${badge(reminder.status)}</div><div class="muted">${reminder.due_at ? `Due ${formatDateTime(reminder.due_at)}` : "No due date"}</div></div>`).join("") : `<div class="empty-state">No reminders for this customer.</div>`}
          </div>
        </section>
        <section class="section-card">
          <div class="detail-header">
            <div>
              <h3>Tracked Chemical Spend</h3>
              <p class="panel-subtitle">Showing the last 4 visits by default. Expand to see the full last 90 days.</p>
            </div>
            ${visits90d.length > 4 ? `<button class="button button-secondary" id="customer-visits-toggle">${state.selections.customerVisitsExpanded ? "Show Last 4 Visits" : "Show 90 Day History"}</button>` : ""}
          </div>
          <div class="event-list">
            ${visibleVisits.length ? visibleVisits.map((visit) => `
              <div class="item-card">
                <div class="item-card-header">
                  <strong>${escapeHtml(visit.pool_name || `Pool ${visit.pool_id}`)}</strong>
                  <span class="dense">${escapeHtml(currency(visit.visit_estimated_cost) || "$0.00")}</span>
                </div>
                <div class="muted">${formatDateTime(visit.service_date)}</div>
                <div class="chem-list">
                  ${(visit.chemicals || []).map((chem) => `
                    <div class="chem-chip">
                      <span>${escapeHtml(chem.description || chem.dosage_key || "Chemical")}${chem.quantity != null ? ` · ${escapeHtml(String(chem.quantity))} ${escapeHtml(chem.unit_of_measure || "")}` : ""}</span>
                      <span class="dense">${escapeHtml(currency(chem.estimated_cost) || "$0.00")}</span>
                    </div>
                  `).join("")}
                </div>
              </div>
            `).join("") : `<div class="empty-state">No chemical spend history available for this customer in the last 90 days.</div>`}
          </div>
        </section>
      </div>
      <aside class="profile-side">
        <section class="section-card">
          <h3>Chemical Spend Summary</h3>
          <div class="meta-stack">
            <div class="meta-row"><span>30 Day Chemical Spend</span><strong>${escapeHtml(currency(spend.cost_30d) || "$0.00")}</strong></div>
            <div class="meta-row"><span>60 Day Chemical Spend</span><strong>${escapeHtml(currency(spend.cost_60d) || "$0.00")}</strong></div>
            <div class="meta-row"><span>90 Day Chemical Spend</span><strong>${escapeHtml(currency(spend.cost_90d) || "$0.00")}</strong></div>
            <div class="meta-row"><span>Average Cost Per Visit (90d)</span><strong>${escapeHtml(currency(avgVisitCost90d) || "$0.00")}</strong></div>
            <div class="meta-row"><span>Latest Visit Chemical Cost</span><strong>${escapeHtml(currency(latestVisitCost) || "$0.00")}</strong></div>
            <div class="meta-row"><span>Latest Dose Date</span><strong>${formatDateTime(spend.latest_dose_date)}</strong></div>
          </div>
        </section>
        <section class="section-card">
          <h3>Tracked Alerts</h3>
          <div class="event-list">
            ${detail.alerts.length ? detail.alerts.map((alert) => `<div class="item-card is-clickable" data-alert-id="${escapeHtml(alert.id)}"><div class="item-card-header"><strong>${escapeHtml(alert.title)}</strong>${badge(alert.status)}</div><div class="muted">${escapeHtml(alert.summary || "")}</div></div>`).join("") : `<div class="empty-state">No tracked alerts for this customer.</div>`}
          </div>
        </section>
      </aside>
    </div>
  `;
  els.mainPanel.querySelectorAll("[data-chart-range]").forEach((el) => {
    el.onclick = async () => {
      state.selections.customerChartDays = Number(el.dataset.chartRange);
      await loadCustomerProfile();
    };
  });
  const visitToggle = document.getElementById("customer-visits-toggle");
  if (visitToggle) {
    visitToggle.onclick = async () => {
      state.selections.customerVisitsExpanded = !state.selections.customerVisitsExpanded;
      await loadCustomerProfile();
    };
  }
  wireNavigationTargets(els.mainPanel);
}

async function loadTechnicians() {
  const result = await api(`/api/technicians${qs(filters.technicians)}`);
  state.data.technicians = result;
  if (!state.selections.techId && result.items[0]) state.selections.techId = result.items[0].tech_id;
  renderTechnicians();
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
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>Technician Profiles</h3>
        <p class="muted">Open any technician to see a full-page route profile with weekday sections, timing signals, spend, alerts, and reminders tied to assigned pools.</p>
      </section>
    </div>
  `;
  wireNavigationTargets(els.mainPanel);
}

async function loadTechnicianDetail(techId) {
  const detail = await api(`/api/technicians/${encodeURIComponent(techId)}`);
  const item = detail.item;
  const spend = detail.chemical_spend_summary || {};
  const assignments = detail.service_locations || [];
  const assignmentGroups = groupAssignmentsByDay(assignments);
  els.detailPanel.innerHTML = `
    <div class="detail-stack">
      <section class="detail-card">
        <h3>${escapeHtml(item.tech_name)}</h3>
        <div class="meta-stack">
          <div class="meta-row"><span>Role</span><strong>${escapeHtml(item.role_type || "—")}</strong></div>
          <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
          <div class="meta-row"><span>Current Assignments</span><strong>${escapeHtml(item.service_location_count)}</strong></div>
          <div class="meta-row"><span>Recent Route Activity</span><strong>${escapeHtml(item.route_stop_count_30d)}</strong></div>
          <div class="meta-row"><span>Spend This Month</span><strong>${escapeHtml(currency(spend.cost_month_to_date) || "$0.00")}</strong></div>
          <div class="meta-row"><span>Spend Last 30 Days</span><strong>${escapeHtml(currency(spend.cost_30d) || "$0.00")}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Current Assignments</h3>
        <div class="event-list">${assignmentGroups.map((group) => `
          <section class="day-group">
            <div class="day-group-header">${escapeHtml(group.dayLabel)}</div>
            <div class="event-list">
              ${group.items.map((location) => {
                const place = [location.city, location.state].filter(Boolean).join(", ");
                const route = [location.frequency, location.sequence != null ? `Stop ${location.sequence}` : ""].filter(Boolean).join(" · ");
                return `<div class="item-card">
                  <strong>${escapeHtml(location.customer_name || location.source_customer_id || "Customer")}</strong>
                  <div class="muted">${escapeHtml(location.address || location.source_location_id || "Location")}${place ? ` · ${escapeHtml(place)}` : ""}</div>
                  <div class="muted">${escapeHtml(location.customer_status || (location.is_operationally_active ? "active" : "—"))}${route ? ` · ${escapeHtml(route)}` : ""}</div>
                </div>`;
              }).join("")}
            </div>
          </section>
        `).join("") || `<div class="empty-state">No current assignments.</div>`}</div>
      </section>
    </div>
  `;
}

async function loadTechnicianProfile() {
  const techId = state.selections.techId;
  if (!techId) {
    els.mainPanel.innerHTML = `<div class="empty-state">Select a technician first.</div>`;
    return;
  }
  const detail = await api(`/api/technicians/${encodeURIComponent(techId)}`);
  renderTechnicianProfile(detail);
}

function renderTechnicianProfile(detail) {
  const item = detail.item;
  const spend = detail.chemical_spend_summary || {};
  const timing = detail.route_timing_summary || {};
  const assignments = detail.service_locations || [];
  const assignmentGroups = groupAssignmentsByDay(assignments);
  const alerts = detail.associated_alerts || [];
  const reminders = detail.associated_reminders || [];

  els.viewKicker.textContent = "Technician";
  els.viewTitle.textContent = item.tech_name;

  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>${escapeHtml(item.tech_name)}</h3>
      <p class="panel-subtitle">Route performance, spend, assignments, and workflow load tied to this technician’s pools.</p>
      <div class="stat-grid">
        <article class="stat-card"><span class="muted">Current Assignments</span><strong>${escapeHtml(item.service_location_count)}</strong></article>
        <article class="stat-card"><span class="muted">Avg Time Per Pool</span><strong>${escapeHtml(formatAxisValue(timing.avg_minutes_per_pool_30d || 0))} min</strong></article>
        <article class="stat-card"><span class="muted">Late Starts</span><strong>${escapeHtml(timing.late_start_count_30d || 0)}</strong></article>
        <article class="stat-card"><span class="muted">Long Stops &gt; 45m</span><strong>${escapeHtml(timing.long_stop_count_30d || 0)}</strong></article>
        <article class="stat-card"><span class="muted">Short Stops &lt; 10m</span><strong>${escapeHtml(timing.short_stop_count_30d || 0)}</strong></article>
        <article class="stat-card"><span class="muted">Avg Spend Per Pool</span><strong>${escapeHtml(currency(spend.avg_spend_per_pool_30d) || "$0.00")}</strong></article>
        <article class="stat-card"><span class="muted">Spend This Month</span><strong>${escapeHtml(currency(spend.cost_month_to_date) || "$0.00")}</strong></article>
        <article class="stat-card"><span class="muted">Spend Last 30 Days</span><strong>${escapeHtml(currency(spend.cost_30d) || "$0.00")}</strong></article>
      </div>
    </section>
    <section class="section-card">
      <h3>Technician Snapshot</h3>
      <div class="meta-stack">
        <div class="meta-row"><span>Role</span><strong>${escapeHtml(item.role_type || "—")}</strong></div>
        <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
        <div class="meta-row"><span>Recent Route Activity</span><strong>${escapeHtml(item.route_stop_count_30d)}</strong></div>
        <div class="meta-row"><span>Earliest Route Start (30d)</span><strong>${formatDateTime(timing.earliest_route_start_30d)}</strong></div>
        <div class="meta-row"><span>Latest Route Start (30d)</span><strong>${formatDateTime(timing.latest_route_start_30d)}</strong></div>
      </div>
    </section>
    <section class="section-card">
      <h3>Assignments By Day</h3>
      <div class="event-list">${assignmentGroups.map((group) => `
        <section class="day-group">
          <div class="day-group-header">
            <span>${escapeHtml(group.dayLabel)}</span>
            <span class="day-group-count">${escapeHtml(group.items.length)} pools</span>
          </div>
          <div class="event-list">
            ${group.items.map((location) => {
              const place = [location.city, location.state].filter(Boolean).join(", ");
              const customerId = location.customer_id != null ? String(location.customer_id) : "";
              const pools = Array.isArray(location.pools) ? location.pools : [];
              return `<div class="item-card assignment-card${customerId ? " is-clickable" : ""}"${customerId ? ` data-customer-id="${escapeHtml(customerId)}"` : ""}>
                <div class="item-card-header">
                  <strong>${escapeHtml(location.customer_name || location.source_customer_id || "Customer")}</strong>
                  <span class="dense">${escapeHtml(pools.length ? `${pools.length} pool${pools.length === 1 ? "" : "s"}` : "No pools")}</span>
                </div>
                <div class="muted">${escapeHtml(location.address || location.source_location_id || "Location")}${place ? ` · ${escapeHtml(place)}` : ""}</div>
                <div class="muted">${escapeHtml(location.customer_status || (location.is_operationally_active ? "active" : "—"))}${location.frequency ? ` · ${escapeHtml(location.frequency)}` : ""}</div>
                <div class="chem-list">
                  ${pools.length ? pools.map((pool) => `
                    <div class="chem-chip">
                      <span>${escapeHtml(pool.pool_name || `Pool ${pool.pool_id}`)}</span>
                      <span class="dense">${escapeHtml(currency(pool.spend_30d) || "$0.00")} / 30d · ${escapeHtml(currency(pool.spend_month_to_date) || "$0.00")} MTD</span>
                    </div>
                  `).join("") : `<div class="muted">No pool spend history linked yet.</div>`}
                </div>
              </div>`;
            }).join("")}
          </div>
        </section>
      `).join("") || `<div class="empty-state">No current assignments.</div>`}</div>
    </section>
    <section class="section-card">
      <h3>Associated Alerts</h3>
      <div class="event-list">
        ${alerts.map((alert) => `<div class="item-card is-clickable" data-alert-id="${escapeHtml(alert.id)}"><div class="item-card-header"><strong>${escapeHtml(alert.title)}</strong><span class="dense">${formatDateTime(alert.last_detected_at)}</span></div><div class="muted">${escapeHtml(alert.customer_name || "No customer")} · ${escapeHtml(alert.pool_name || "No pool")}</div><div class="muted">${escapeHtml(alert.summary || "")}</div></div>`).join("") || `<div class="empty-state">No active alerts tied to this technician’s pools.</div>`}
      </div>
    </section>
    <section class="section-card">
      <h3>Associated Reminders</h3>
      <div class="event-list">
        ${reminders.map((reminder) => `<div class="item-card is-clickable" data-reminder-id="${escapeHtml(reminder.id)}"><div class="item-card-header"><strong>${escapeHtml(reminder.title)}</strong><span class="dense">${reminder.due_at ? formatDateTime(reminder.due_at) : "No due date"}</span></div><div class="muted">${escapeHtml(reminder.customer_name || "No customer")} · ${escapeHtml(reminder.pool_name || "No pool")}</div><div class="muted">${escapeHtml(reminder.summary || "")}</div></div>`).join("") || `<div class="empty-state">No active reminders tied to this technician’s pools.</div>`}
      </div>
    </section>
  `;

  wireNavigationTargets(els.mainPanel);
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
