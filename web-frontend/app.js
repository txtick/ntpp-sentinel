const DEFAULT_CUSTOMER_CHART_POLICY = {
  default_days: 90,
  range_days: [30, 90, 180, 365],
  hidden_metrics: ["total_chlorine", "combined_chlorine"],
  sparse_metrics: [],
  required_every_visit_metrics: [
    "free_chlorine",
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
    "free_chlorine",
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
    free_chlorine: 5,
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
    ph: 2,
    lsi: 2,
  },
  metric_labels: {
    ph: "pH",
    free_chlorine: "Free Chlorine",
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

function isoDate(value) {
  return value.toISOString().slice(0, 10);
}

function defaultLaborWeek() {
  const now = new Date();
  const localMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const sundayOffset = localMidnight.getDay();
  const start = new Date(localMidnight);
  start.setDate(localMidnight.getDate() - sundayOffset);
  const end = new Date(start);
  end.setDate(start.getDate() + 6);
  return {
    start_date: isoDate(start),
    end_date: isoDate(end),
  };
}

const DEFAULT_LABOR_FILTERS = {
  ...defaultLaborWeek(),
  include_salary: 0,
};

const state = {
  view: "home",
  actor: "",
  auth: {
    enabled: false,
    authenticated: false,
    loginUrl: "/auth/google/start",
    user: null,
  },
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
    weather: null,
    alerts: null,
    customers: null,
    technicians: null,
    labor: null,
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
  sessionCard: document.getElementById("session-card"),
  sessionSummary: document.getElementById("session-summary"),
  loginButton: document.getElementById("login-button"),
  logoutButton: document.getElementById("logout-button"),
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
  labor: { kicker: "Labor", title: "Weekly Payroll Prep" },
  reminders: { kicker: "Reminders", title: "Follow-Up Queue" },
};

const filters = {
  alerts: { status: "", category: "", severity: "", rule_code: "", search: "", limit: 20, offset: 0 },
  customers: { search: "", operational_only: 1, status: "", limit: 20, offset: 0 },
  technicians: {
    search: "",
    active_only: 0,
    with_current_assignments_only: 1,
    with_recent_route_activity_only: 0,
    field_only: 0,
    role_type: "",
    limit: 40,
  },
  labor: { ...DEFAULT_LABOR_FILTERS },
  reminders: { status: "", assigned_to: "", source_type: "", overdue_only: 0, search: "", limit: 40 },
};

function cloneFilters() {
  return JSON.parse(JSON.stringify(filters));
}

function snapshotAppState() {
  return {
    view: state.view,
    selections: { ...state.selections },
    filters: cloneFilters(),
  };
}

function restoreAppState(snapshot = {}) {
  state.view = snapshot.view || "home";
  Object.assign(state.selections, snapshot.selections || {});
  const nextFilters = snapshot.filters || {};
  Object.keys(filters).forEach((key) => {
    Object.assign(filters[key], nextFilters[key] || {});
  });
}

function pushBrowserState() {
  window.history.pushState(snapshotAppState(), "", "");
}

function renderSessionCard() {
  if (!els.sessionCard) return;
  if (!state.auth.enabled) {
    els.sessionSummary.textContent = "Dashboard auth is not configured. Secret-based local development is still available.";
    els.loginButton.hidden = true;
    els.logoutButton.hidden = true;
    return;
  }
  if (state.auth.authenticated && state.auth.user) {
    const name = state.auth.user.name || state.auth.user.email || "Signed in";
    const email = state.auth.user.email ? ` (${state.auth.user.email})` : "";
    els.sessionSummary.textContent = `Signed in as ${name}${email}`;
    els.loginButton.hidden = true;
    els.logoutButton.hidden = false;
    return;
  }
  els.sessionSummary.textContent = "Sign in with your North Texas Pool Pros Google Workspace account to use the dashboard.";
  els.loginButton.hidden = false;
  els.logoutButton.hidden = true;
  els.loginButton.href = state.auth.loginUrl || "/auth/google/start";
}

function renderAuthRequired() {
  const loginUrl = state.auth.loginUrl || "/auth/google/start";
  els.mainPanel.innerHTML = `
    <div class="empty-state">
      Sign in is required to use the dashboard.<br /><br />
      <a class="button button-primary" href="${escapeHtml(loginUrl)}">Sign In with Google</a>
    </div>
  `;
}

async function hydrateSession() {
  const response = await fetch("/auth/session", { credentials: "same-origin" });
  const payload = await response.json().catch(() => ({}));
  state.auth.enabled = Boolean(payload.enabled);
  state.auth.authenticated = Boolean(payload.authenticated);
  state.auth.loginUrl = payload.login_url || "/auth/google/start";
  state.auth.user = payload.user || null;
  state.actor = state.auth.user?.actor || state.auth.user?.email || "";
  renderSessionCard();
}

async function init() {
  await hydrateSession();

  document.querySelectorAll(".nav-link").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });

  if (els.logoutButton) {
    els.logoutButton.addEventListener("click", async () => {
      await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
      state.auth.authenticated = false;
      state.auth.user = null;
      state.actor = "";
      renderSessionCard();
      renderAuthRequired();
      showToast("Signed out.");
    });
  }

  document.getElementById("refresh-view").addEventListener("click", () => loadCurrentView(true));

  window.addEventListener("popstate", (event) => {
    restoreAppState(event.state || { view: "home" });
    document.querySelectorAll(".nav-link").forEach((button) => {
      const activeView = ["customer-profile"].includes(state.view)
        ? "customers"
        : ["alert-profile"].includes(state.view)
          ? "alerts"
          : ["technician-profile"].includes(state.view)
            ? "technicians"
            : state.view;
      button.classList.toggle("is-active", button.dataset.view === activeView);
    });
    const meta = viewMeta[state.view];
    els.viewKicker.textContent = meta.kicker;
    els.viewTitle.textContent = meta.title;
    renderFilters();
    loadCurrentView(true);
  });

  window.history.replaceState(snapshotAppState(), "", "");
  setView("home", { pushHistory: false });
}

function setView(view, { pushHistory = true } = {}) {
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
  if (pushHistory) pushBrowserState();
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
  const response = await fetch(path, { method, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && data.auth_required) {
      state.auth.enabled = true;
      state.auth.authenticated = false;
      state.auth.loginUrl = data.login_url || "/auth/google/start";
      renderSessionCard();
      renderAuthRequired();
      throw new Error("Dashboard login required.");
    }
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

function isFilterCleanAlert(item = {}) {
  return [
    "filter_clean_trend",
    "filter_clean_missing_psi",
    "freedom_filter_clean_not_scheduled",
  ].includes(String(item.rule_code || "").trim());
}

function filterCleanNotifyToast(payload = {}) {
  if (payload.quote_detected) return "Customer notified. Matching filter clean quote found and reminder completed.";
  if (payload.notification_sent) return "Customer notified and quote reminder saved.";
  if (payload.notification_error) return `Reminder saved, but SMS was not sent: ${payload.notification_error}`;
  return "Quote reminder saved.";
}

function renderPaginationControls(result, filterKey) {
  const limit = Number(filters[filterKey]?.limit || 20);
  const offset = Number(filters[filterKey]?.offset || 0);
  const total = Number(result?.total || 0);
  const page = Math.floor(offset / limit) + 1;
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const disablePrev = offset <= 0;
  const disableNext = offset + limit >= total;
  return `
    <div class="pagination-bar">
      <button class="button button-secondary" data-page-action="prev" data-page-target="${filterKey}" ${disablePrev ? "disabled" : ""}>Previous</button>
      <span class="pagination-label">Page ${page} of ${pageCount}</span>
      <button class="button button-secondary" data-page-action="next" data-page-target="${filterKey}" ${disableNext ? "disabled" : ""}>Next</button>
    </div>
  `;
}

function wirePagination(root = document) {
  root.querySelectorAll("[data-page-action]").forEach((el) => {
    el.onclick = async () => {
      const target = el.dataset.pageTarget;
      const direction = el.dataset.pageAction;
      const limit = Number(filters[target]?.limit || 20);
      const currentOffset = Number(filters[target]?.offset || 0);
      const nextOffset = direction === "next"
        ? currentOffset + limit
        : Math.max(0, currentOffset - limit);
      filters[target].offset = nextOffset;
      if (target === "alerts") {
        await loadAlerts();
      } else if (target === "customers") {
        await loadCustomers();
      }
      pushBrowserState();
    };
  });
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

function formatClockTime(value) {
  if (!value) return "—";
  const raw = String(value).trim();
  const match = raw.match(/^(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$/);
  if (!match) return raw;
  let hour = Number(match[1]);
  const minute = match[2];
  const suffix = hour >= 12 ? "PM" : "AM";
  hour = hour % 12 || 12;
  return `${hour}:${minute} ${suffix}`;
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
  const stickyDetail = !single && ["customers", "technicians", "reminders"].includes(state.view);
  els.contentGrid.classList.toggle("has-sticky-detail", stickyDetail);
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
  if ((raw === "none" || raw === "value") && desc.includes("chlorine")) return "free_chlorine";
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

function chartBoundsForReading(readingKey, values, recommendedHigh) {
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const normalizedKey = normalizeMetricKey(readingKey);

  if (normalizedKey === "ph") {
    const lower = Math.max(6.8, Math.min(rawMin - 0.15, 7.2));
    const upper = Math.min(8.6, Math.max(rawMax + 0.15, 8.0, Number.isFinite(recommendedHigh) ? recommendedHigh : 0));
    if (upper - lower >= 0.2) {
      return { min: lower, max: upper };
    }
  }

  const paddedMin = 0;
  const paddedMax = Number.isFinite(recommendedHigh) && recommendedHigh > 0
    ? recommendedHigh
    : Math.max(rawMax * 1.2, 10);
  return { min: paddedMin, max: paddedMax };
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

function numericOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
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
  if (metadata.rule_description) return metadata.rule_description;
  if (item.category === "revenue" && metadata.opportunity_type === "chemical_cost_review") {
    const observedCost = currency(metadata.observed_count);
    if (observedCost) {
      return `Chemical spend flagged at ${observedCost} in the review window.`;
    }
  }
  return item.summary || "No summary available.";
}

function formatAlertReading(item) {
  const metadata = item.metadata_json || {};
  const readingKey = metadata.reading_key || "";
  // observed_value is a trend/count for process alerts, not an actual reading — only use value
  const observed = numericOrNull(metadata.value);
  const threshold = numericOrNull(metadata.threshold_value);
  if (observed === null) return "";

  const metricLabel = formatMetricLabel({ readingKey, description: metadata.description || "" });
  const observedLabel = formatAxisValue(observed, readingKey);
  if (threshold === null) return `Last ${metricLabel} ${observedLabel}`;

  const thresholdLabel = formatAxisValue(threshold, readingKey);
  const delta = observed - threshold;
  const deltaLabel = `${delta >= 0 ? "+" : ""}${formatAxisValue(delta, readingKey)}`;
  return `Last ${metricLabel} ${observedLabel} vs ${thresholdLabel} (${deltaLabel})`;
}

function formatAlertSubline(item) {
  const metadata = item.metadata_json || {};
  const assignedTech = metadata.assigned_technician?.tech_name;
  const friendlyRule = metadata.rule_description || item.rule_code?.replaceAll("_", " ");
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
  const readingSummary = formatAlertReading(item);
  if (readingSummary) {
    return `${readingSummary}${assignedTech ? ` · tech ${assignedTech}` : ""}`;
  }
  if (assignedTech) return `tech ${assignedTech}`;
  return friendlyRule;
}

function alertRelevantSeries(detail) {
  const keys = new Set((detail.alert_chart_keys || []).map((key) => normalizeMetricKey(key)));
  const allSeries = groupChemistrySeries(detail.chemistry_history || []);
  if (!keys.size) return [];
  return allSeries.filter((seriesItem) => keys.has(normalizeMetricKey(seriesItem.readingKey)));
}

function renderAlertCharts(detail, mode = "detail") {
  const series = filterSeriesByDays(alertRelevantSeries(detail), customerChartPolicy().default_days || 90);
  if (!series.length) {
    return `<div class="empty-state">No matching chemistry chart data for this alert yet.</div>`;
  }
  const gridClass = mode === "profile" ? "chart-grid chart-grid-alert" : "chart-grid chart-grid-alert";
  return `
    <div class="${gridClass}">
      ${series.map((seriesItem) => {
        const latest = seriesItem.points[seriesItem.points.length - 1];
        const values = seriesItem.points.map((point) => Number(point.value)).filter((value) => Number.isFinite(value));
        return `
          <article class="chart-card chart-card-alert">
            <div>
              <h4>${escapeHtml(formatMetricLabel(seriesItem))}</h4>
              ${seriesItem.poolName && seriesItem.poolName !== "Pool" ? `<div class="muted">${escapeHtml(seriesItem.poolName)}</div>` : ""}
            </div>
            ${buildLineChart(seriesItem, { compact: true })}
            <div class="chart-caption">
              <span>Latest ${escapeHtml(formatAxisValue(latest?.value, seriesItem.readingKey))}</span>
              <span>Min ${escapeHtml(formatAxisValue(values.length ? Math.min(...values) : "—", seriesItem.readingKey))} · Max ${escapeHtml(formatAxisValue(values.length ? Math.max(...values) : "—", seriesItem.readingKey))}</span>
            </div>
          </article>
        `;
      }).join("")}
    </div>
  `;
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
      const customerId = Number(el.dataset.customerId);
      state.selections.customerId = customerId;
      state.selections.customerChartDays = customerChartPolicy().default_days || DEFAULT_CUSTOMER_CHART_POLICY.default_days;
      state.selections.customerVisitsExpanded = false;
      setView("customer-profile");
    };
  });
  root.querySelectorAll("[data-customer-open-profile]").forEach((el) => {
    el.onclick = (event) => {
      event.stopPropagation();
      state.selections.customerId = Number(el.dataset.customerOpenProfile);
      state.selections.customerChartDays = customerChartPolicy().default_days || DEFAULT_CUSTOMER_CHART_POLICY.default_days;
      state.selections.customerVisitsExpanded = false;
      setView("customer-profile");
    };
  });
  root.querySelectorAll("[data-alert-id]").forEach((el) => {
    el.onclick = () => {
      const alertId = Number(el.dataset.alertId);
      state.selections.alertId = alertId;
      setView("alert-profile");
    };
  });
  root.querySelectorAll("[data-alert-open-profile]").forEach((el) => {
    el.onclick = (event) => {
      event.stopPropagation();
      state.selections.alertId = Number(el.dataset.alertOpenProfile);
      setView("alert-profile");
    };
  });
  root.querySelectorAll("[data-tech-id]").forEach((el) => {
    el.onclick = () => {
      const techId = el.dataset.techId;
      state.selections.techId = techId;
      setView("technician-profile");
    };
  });
  root.querySelectorAll("[data-tech-open-profile]").forEach((el) => {
    el.onclick = (event) => {
      event.stopPropagation();
      state.selections.techId = el.dataset.techOpenProfile;
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
  root.querySelectorAll("[data-home-nav]").forEach((el) => {
    el.onclick = () => {
      const target = el.dataset.homeNav;
      const status = el.dataset.homeStatus || "";
      const category = el.dataset.homeCategory || "";
      const severity = el.dataset.homeSeverity || "";
      const overdueOnly = Number(el.dataset.homeOverdueOnly || 0);
      if (target === "alerts") {
        filters.alerts.status = status;
        filters.alerts.category = category;
        filters.alerts.severity = severity;
        filters.alerts.search = "";
        filters.alerts.rule_code = "";
        filters.alerts.offset = 0;
        state.selections.alertId = null;
        setView("alerts");
        return;
      }
      if (target === "customers") {
        filters.customers.operational_only = 1;
        filters.customers.status = status;
        filters.customers.search = "";
        filters.customers.offset = 0;
        setView("customers");
        return;
      }
      if (target === "reminders") {
        filters.reminders.status = status;
        filters.reminders.overdue_only = overdueOnly;
        filters.reminders.search = "";
        setView("reminders");
      }
    };
  });
}

async function selectAlert(alertId, { pushHistory = true } = {}) {
  state.selections.alertId = Number(alertId);
  renderAlerts();
  await loadAlertDetail(state.selections.alertId);
  if (pushHistory) pushBrowserState();
}

function renderFilters() {
  if (state.view === "home") {
    setLayout("split");
    els.filters.innerHTML = `<div class="muted">Live snapshot of backend summary, recent alerts, and reminder pressure.</div>`;
    return;
  }

  if (state.view === "alerts") {
    setLayout("single");
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
      <label class="filter-chip"><span>Severity</span>
        <select id="filter-alert-severity">
          <option value="">All</option>
          <option value="critical" ${f.severity === "critical" ? "selected" : ""}>Critical</option>
          <option value="warning" ${f.severity === "warning" ? "selected" : ""}>Warning</option>
          <option value="info" ${f.severity === "info" ? "selected" : ""}>Info</option>
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
    document.getElementById("filter-alert-severity").onchange = (e) => {
      filters.alerts.severity = e.target.value;
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
    setLayout("single");
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
    setLayout("single");
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

  if (state.view === "labor") {
    setLayout("single");
    const f = filters.labor;
    els.filters.innerHTML = `
      <label class="filter-chip"><span>Week Start</span><input id="filter-labor-start" type="date" value="${escapeHtml(f.start_date)}" /></label>
      <label class="filter-chip"><span>Week End</span><input id="filter-labor-end" type="date" value="${escapeHtml(f.end_date)}" /></label>
      <label class="filter-chip"><span>Salary Techs</span>
        <select id="filter-labor-salary">
          <option value="0" ${!f.include_salary ? "selected" : ""}>Hide</option>
          <option value="1" ${f.include_salary ? "selected" : ""}>Show</option>
        </select>
      </label>
    `;
    document.getElementById("filter-labor-start").onchange = (e) => {
      filters.labor.start_date = e.target.value;
      loadLabor(true);
    };
    document.getElementById("filter-labor-end").onchange = (e) => {
      filters.labor.end_date = e.target.value;
      loadLabor(true);
    };
    document.getElementById("filter-labor-salary").onchange = (e) => {
      filters.labor.include_salary = Number(e.target.value);
      loadLabor(true);
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
    if (state.view === "labor") await loadLabor(force);
    if (state.view === "reminders") await loadReminders(force);
    setStatus("Live", "info");
  } catch (error) {
    setStatus("Error", "critical");
    els.mainPanel.innerHTML = `<div class="empty-state">Could not load this view.<br /><br />${escapeHtml(error.message)}</div>`;
    showToast(error.message, 4400);
  }
}

async function loadHome() {
  const [summary, alerts, reminders, weather] = await Promise.all([
    api("/api/home/summary"),
    api("/api/alerts?limit=6"),
    api("/api/reminders?limit=6"),
    api("/api/weather").catch(() => null),
  ]);
  state.data.home = { summary, alerts, reminders };
  state.data.weather = weather;
  renderHome();
}

const WMO_LABELS = {
  0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Overcast",
  45: "Foggy", 48: "Icy Fog",
  51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
  61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
  71: "Light Snow", 73: "Snow", 75: "Heavy Snow",
  80: "Showers", 81: "Heavy Showers", 82: "Downpours",
  95: "Thunderstorm", 96: "T-Storm + Hail", 99: "Severe T-Storm",
};

function wmoLabel(code) {
  return WMO_LABELS[code] ?? "—";
}

function windLevel(mph) {
  if (mph == null) return { label: "—", color: "var(--muted)" };
  const v = Math.round(mph);
  if (mph < 15) return { label: `${v} mph`, color: "var(--ink)" };
  if (mph < 25) return { label: `${v} mph`, color: "var(--gold)" };
  if (mph < 35) return { label: `${v} mph`, color: "#d97706" };
  return { label: `${v} mph ⚠`, color: "var(--danger)" };
}

function dustLevel(ugm3) {
  if (ugm3 == null) return { label: "—", color: "var(--muted)" };
  if (ugm3 < 25)  return { label: "Low", color: "var(--ink)" };
  if (ugm3 < 75)  return { label: "Elevated", color: "var(--gold)" };
  if (ugm3 < 200) return { label: "High", color: "#d97706" };
  return { label: "Saharan ⚠", color: "var(--danger)" };
}

function pollenLevel(index) {
  // Tomorrow.io 0–5 index: 0=None, 1=Very Low, 2=Low, 3=Medium, 4=High, 5=Very High
  if (index == null) return { label: "—", color: "var(--muted)" };
  if (index === 0)   return { label: "None", color: "var(--muted)" };
  if (index <= 1)    return { label: "Very Low", color: "var(--ink)" };
  if (index <= 2)    return { label: "Low", color: "var(--ink)" };
  if (index <= 3)    return { label: "Moderate", color: "var(--gold)" };
  if (index <= 4)    return { label: "High", color: "#d97706" };
  return { label: "Very High ⚠", color: "var(--danger)" };
}

function algaeRisk(tempF) {
  if (tempF == null) return "—";
  if (tempF < 60) return "Low — Algae Dormant";
  if (tempF < 65) return "Low-Mod — Spring Warming";
  if (tempF < 70) return "Moderate — Monitor Closely";
  if (tempF < 78) return "High — Algae Season";
  return "High — Peak Season";
}

function pollenNote() {
  const m = new Date().getMonth() + 1;
  if (m === 1)  return "Mountain cedar — very high";
  if (m === 2)  return "Cedar ending · elm & oak starting";
  if (m === 3)  return "Oak, elm, ash — high";
  if (m === 4)  return "Oak continuing · grass building";
  if (m === 5)  return "Grass pollen — moderate";
  if (m === 6)  return "Grass pollen — high";
  if (m === 7)  return "Grass peak · ragweed starting";
  if (m === 8)  return "Ragweed building — high";
  if (m === 9)  return "Ragweed peak — very high";
  if (m === 10) return "Ragweed ending · mold possible";
  if (m === 11) return "Low — cedar starting late month";
  return "Cedar season beginning";
}

function renderWeatherWidget() {
  const w = state.data.weather;
  if (!w) return `<section class="detail-card"><h3>Weather</h3><p class="muted">Weather data unavailable.</p></section>`;

  const cur = w.current || {};
  const daily = w.daily || {};
  const waterTemp = w.estimated_water_temp_f;

  const _now = new Date();
  const todayLocal = [_now.getFullYear(), String(_now.getMonth() + 1).padStart(2, "0"), String(_now.getDate()).padStart(2, "0")].join("-");
  const forecastDays = (daily.time || [])
    .map((d, i) => ({
      date: d,
      max: daily.temperature_2m_max?.[i],
      min: daily.temperature_2m_min?.[i],
      code: daily.weather_code?.[i],
      precip: daily.precipitation_sum?.[i],
      wind: daily.wind_speed_10m_max?.[i],
    }))
    .filter((d) => d.date >= todayLocal)
    .slice(0, 6);

  const _now2 = new Date();
  const todayLocal2 = [_now2.getFullYear(), String(_now2.getMonth() + 1).padStart(2, "0"), String(_now2.getDate()).padStart(2, "0")].join("-");
  const envDays = (w.environmental || []).filter((d) => d.date <= todayLocal2).slice(-7);
  const envGridHtml = envDays.length ? `
    <div style="margin-top:14px">
      <div class="muted" style="font-size:0.75em;margin-bottom:6px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase">Past 7 Days · Pool Environment</div>
      <div style="display:grid;grid-template-columns:auto 1fr 1fr 1fr;gap:3px 10px;align-items:center;font-size:0.78em">
        <div class="muted">Date</div><div class="muted">Wind</div><div class="muted">Dust</div><div class="muted">Pollen</div>
        ${envDays.map((d) => {
          const isToday = d.date === todayLocal2;
          const label = isToday ? "Today" : new Date(d.date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
          const w2 = windLevel(d.max_wind);
          const du = dustLevel(d.max_dust);
          const po = pollenLevel(d.max_pollen);
          const rowStyle = isToday ? "font-weight:600" : "";
          return `<div style="${rowStyle};color:var(--ink)">${label}</div>
                  <div style="color:${w2.color}">${w2.label}</div>
                  <div style="color:${du.color}">${du.label}</div>
                  <div style="color:${po.color}">${po.label}</div>`;
        }).join("")}
      </div>
    </div>` : "";

  const forecastHtml = forecastDays.map((d) => {
    const label = new Date(d.date + "T12:00:00").toLocaleDateString("en-US", { weekday: "short" });
    const precipLine = d.precip > 0.01 ? `<div class="muted">${d.precip.toFixed(2)}"</div>` : "";
    const wl = windLevel(d.wind);
    const windLine = d.wind >= 15 ? `<div style="color:${wl.color};font-size:0.85em">${wl.label}</div>` : "";
    return `<div style="background:var(--bg-strong);border-radius:8px;padding:6px 4px;line-height:1.4">
      <div style="font-weight:600">${label}</div>
      <div style="font-size:0.85em">${wmoLabel(d.code)}</div>
      <div>${Math.round(d.max ?? 0)}° / ${Math.round(d.min ?? 0)}°</div>
      ${precipLine}${windLine}
    </div>`;
  }).join("");

  return `
    <section class="detail-card">
      <h3>Weather <span class="muted" style="font-weight:400;font-size:0.8em">· North TX</span></h3>
      <div class="meta-stack">
        <div class="meta-row"><span>Now</span><strong>${Math.round(cur.temperature_2m ?? 0)}°F · ${wmoLabel(cur.weather_code)}</strong></div>
        <div class="meta-row"><span>Feels Like</span><strong>${Math.round(cur.apparent_temperature ?? 0)}°F</strong></div>
        <div class="meta-row"><span>UV Index</span><strong>${cur.uv_index ?? "—"}</strong></div>
        <div class="meta-row"><span>Wind</span><strong>${Math.round(cur.wind_speed_10m ?? 0)} mph</strong></div>
        <div class="meta-row"><span>${w.water_temp_source === "measured" ? "Avg Water Temp (7d)" : "Est. Water Temp"}</span><strong>${waterTemp != null ? `${waterTemp}°F` : "—"}</strong></div>
        <div class="meta-row"><span>Algae Risk</span><strong>${algaeRisk(waterTemp)}</strong></div>
        <div class="meta-row"><span>Pollen Season</span><strong>${pollenNote()}</strong></div>
      </div>
      ${envGridHtml}
      <div style="margin-top:12px;display:grid;grid-template-columns:repeat(6,1fr);gap:5px;text-align:center;font-size:0.78em">
        ${forecastHtml}
      </div>
    </section>
  `;
}

function renderHome() {
  const payload = state.data.home.summary.summary || {};
  const alertCounts = payload.tracked_alert_counts_by_status || [];
  const reminderCounts = payload.reminder_counts || {};
  const cards = [
    { label: "Active Customers", value: payload.active_customer_count, target: "customers", status: "active" },
    { label: "Active Pools", value: payload.active_pool_count, target: "customers", status: "active" },
    { label: "Customers With Current Alerts", value: payload.customers_with_current_alerts, target: "alerts", status: "open" },
    { label: "Critical Current Alerts", value: payload.critical_current_alert_count, target: "alerts", status: "open", severity: "critical" },
    { label: "Trend Alerts", value: payload.chemistry_trend_alert_count, target: "alerts", category: "process" },
    { label: "Revenue Opportunities", value: payload.revenue_opportunity_count, target: "alerts", category: "revenue" },
    { label: "Tracked Open Reminders", value: reminderCounts.open_reminder_count, target: "reminders", status: "open" },
    { label: "Overdue Reminders", value: reminderCounts.overdue_reminder_count, target: "reminders", overdueOnly: 1 },
  ];

  els.mainPanel.innerHTML = `
    <div class="stat-grid">
      ${cards.map((card) => `<article class="stat-card is-clickable" data-home-nav="${escapeHtml(card.target)}" data-home-status="${escapeHtml(card.status || "")}" data-home-category="${escapeHtml(card.category || "")}" data-home-severity="${escapeHtml(card.severity || "")}" data-home-overdue-only="${escapeHtml(card.overdueOnly || 0)}"><span class="muted">${card.label}</span><strong>${escapeHtml(card.value ?? "0")}</strong></article>`).join("")}
    </div>
    <section class="section-card">
      <h3>Tracked Alert Status Mix</h3>
      <p class="panel-subtitle">Durable workflow state from the web backend, not raw query output.</p>
      <div class="list-grid">
        ${alertCounts.map((item) => `<div class="item-card is-clickable" data-home-nav="alerts" data-home-status="${escapeHtml(item.status || "")}" data-home-category="${escapeHtml(item.category || "")}" data-home-severity=""><div class="item-card-header"><strong>${escapeHtml(item.category)}</strong>${badge(item.status)}</div><div class="muted">${escapeHtml(item.count)}</div></div>`).join("") || `<div class="empty-state">No tracked alerts yet.</div>`}
      </div>
    </section>
    <section class="section-card">
      <h3>Recent Alerts</h3>
      <div class="item-list">
        ${state.data.home.alerts.items.map((item) => `<article class="item-card is-clickable" data-alert-open-profile="${escapeHtml(item.id)}"><div class="item-card-header"><strong>${escapeHtml(item.title)}</strong><div>${badge(item.category)} ${badge(item.severity)} ${badge(item.status)}</div></div><div class="muted">${formatDateTime(item.last_detected_at)}</div></article>`).join("")}
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
      ${renderWeatherWidget()}
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

async function loadAlerts(resetOffset = false) {
  if (resetOffset) filters.alerts.offset = 0;
  const result = await api(`/api/alerts${qs(filters.alerts)}`);
  state.data.alerts = result;
  renderAlerts();
}

function renderAlerts() {
  const result = state.data.alerts;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Alert Queue</h3>
      <p class="panel-subtitle">${escapeHtml(result.total)} tracked items in this filter set. Click any alert to open the full detail page.</p>
      <div class="item-list">
        ${result.items.map((item) => `
          <article class="item-card is-clickable" data-alert-id="${item.id}">
            <div class="item-card-header">
              <div>
                <h4>${escapeHtml(item.title)}</h4>
                <div class="muted">${escapeHtml(formatAlertSummary(item))}</div>
              </div>
              <div class="meta-stack">
                ${alertReasonBadge(item)} ${badge(item.severity)} ${badge(item.status)}
                <div class="row-actions">
                  ${isFilterCleanAlert(item) ? `<button class="button button-secondary button-inline" data-alert-row-action="notify" data-alert-id="${item.id}">Notify</button>` : ""}
                  <button class="button button-secondary button-inline" data-alert-row-action="ack" data-alert-id="${item.id}">Ack</button>
                  <button class="button button-danger button-inline" data-alert-row-action="resolve" data-alert-id="${item.id}">Resolve</button>
                </div>
              </div>
            </div>
            <div class="meta-row">
              <span>${escapeHtml(formatAlertSubline(item))}</span>
              <span>${formatDateTime(item.last_detected_at)}</span>
            </div>
          </article>
        `).join("")}
      </div>
      ${renderPaginationControls(result, "alerts")}
    </section>
  `;
  wireNavigationTargets(els.mainPanel);
  wirePagination(els.mainPanel);
  wireAlertRowActions(els.mainPanel);
}

function wireAlertRowActions(root = document) {
  root.querySelectorAll("[data-alert-row-action]").forEach((button) => {
    button.onclick = async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const alertId = Number(button.dataset.alertId);
      const action = button.dataset.alertRowAction;
      if (!alertId || !action) return;
      if (action === "notify") {
        await mutate(async () => {
          const params = new URLSearchParams({
            actor: state.actor || "ui",
            assigned_to: state.actor || "ui",
          });
          const payload = await api(`/api/alerts/${alertId}/notify-customer?${params.toString()}`, { method: "POST", auth: true });
          await loadAlerts();
          if (state.selections.alertId === alertId) {
            await loadAlertDetail(alertId);
          }
          showToast(filterCleanNotifyToast(payload), 4200);
        });
        return;
      }
      const endpoint = action === "resolve" ? "resolve" : "ack";
      const successMessage = action === "resolve" ? "Alert resolved." : "Alert acknowledged.";
      await mutate(async () => {
        await api(`/api/alerts/${alertId}/${endpoint}?actor=${encodeURIComponent(state.actor || "ui")}`, { method: "POST", auth: true });
        await loadAlerts();
        if (state.selections.alertId === alertId) {
          await loadAlertDetail(alertId);
        }
      }, successMessage);
    };
  });
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
  state.config.customerCharts = mergeCustomerChartPolicy(detail.chart_policy || {});
  const item = detail.item;
  const metadata = item.metadata_json || {};
  const observedCost = currency(metadata.observed_count);
  const thresholdCost = currency(metadata.threshold_value);
  const assignedTech = metadata.assigned_technician?.tech_name || "Unassigned";
  const recentTech = metadata.recent_service_technician?.tech_name || "No recent route stop";
  const assignedTechId = metadata.assigned_technician?.tech_id || metadata.assigned_technician?.technician_id;
  const recentTechId = metadata.recent_service_technician?.tech_id || metadata.recent_service_technician?.technician_id;
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
          <div class="item-card is-clickable" ${item.customer_id ? `data-customer-id="${escapeHtml(item.customer_id)}"` : ""}><div class="meta-row"><span>Customer</span><strong>${escapeHtml(metadata.customer_name || item.customer_id || "—")}</strong></div></div>
          <div class="item-card ${item.customer_id ? "is-clickable" : ""}" ${item.customer_id ? `data-customer-id="${escapeHtml(item.customer_id)}"` : ""}><div class="meta-row"><span>Pool</span><strong>${escapeHtml(metadata.pool_name || item.pool_id || "—")}</strong></div></div>
          <div class="item-card ${assignedTechId ? "is-clickable" : ""}" ${assignedTechId ? `data-tech-id="${escapeHtml(assignedTechId)}"` : ""}><div class="meta-row"><span>Assigned Technician</span><strong>${escapeHtml(assignedTech)}</strong></div></div>
          <div class="item-card ${recentTechId ? "is-clickable" : ""}" ${recentTechId ? `data-tech-id="${escapeHtml(recentTechId)}"` : ""}><div class="meta-row"><span>Recent Service Tech</span><strong>${escapeHtml(recentTech)}</strong></div></div>
          ${observedCost ? `<div class="meta-row"><span>Observed Cost</span><strong>${escapeHtml(observedCost)}</strong></div>` : ""}
          ${thresholdCost ? `<div class="meta-row"><span>Threshold</span><strong>${escapeHtml(thresholdCost)}</strong></div>` : ""}
          <div class="meta-row"><span>Last Detected</span><strong>${formatDateTime(item.last_detected_at)}</strong></div>
          <div class="meta-row"><span>Snoozed Until</span><strong>${formatDateTime(item.snoozed_until)}</strong></div>
        </div>
      </section>
      <section class="detail-card">
        <h3>Related Chemistry</h3>
        ${renderAlertCharts(detail)}
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
          ${isFilterCleanAlert(item) ? `<button class="button button-secondary" id="alert-notify-customer-button">Notify Customer</button>` : ""}
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
  const notifyButton = document.getElementById("alert-notify-customer-button");
  if (notifyButton) {
    notifyButton.onclick = () => mutate(async () => {
      const assignedTo = document.getElementById("alert-reminder-assigned").value.trim();
      const dueAt = toUtcIso(document.getElementById("alert-reminder-due").value);
      const note = document.getElementById("alert-reminder-note").value.trim();
      const params = new URLSearchParams({
        actor: state.actor || "ui",
        assigned_to: assignedTo,
        due_at: dueAt,
        note,
      });
      const payload = await api(`/api/alerts/${item.id}/notify-customer?${params.toString()}`, { method: "POST", auth: true });
      await loadAlertDetail(item.id);
      showToast(filterCleanNotifyToast(payload), 4200);
    });
  }
  wireNavigationTargets(els.detailPanel);
}

function renderAlertProfile(detail) {
  state.config.customerCharts = mergeCustomerChartPolicy(detail.chart_policy || {});
  const item = detail.item;
  const metadata = item.metadata_json || {};
  const observedCost = currency(metadata.observed_count);
  const thresholdCost = currency(metadata.threshold_value);
  const assignedTech = metadata.assigned_technician?.tech_name || "Unassigned";
  const recentTech = metadata.recent_service_technician?.tech_name || "No recent route stop";
  const assignedTechId = metadata.assigned_technician?.tech_id || metadata.assigned_technician?.technician_id;
  const recentTechId = metadata.recent_service_technician?.tech_id || metadata.recent_service_technician?.technician_id;
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
        <article class="stat-card is-clickable" ${item.customer_id ? `data-customer-id="${escapeHtml(item.customer_id)}"` : ""}><span class="muted">Customer</span><strong>${escapeHtml(metadata.customer_name || item.customer_id || "—")}</strong></article>
        <article class="stat-card ${item.customer_id ? "is-clickable" : ""}" ${item.customer_id ? `data-customer-id="${escapeHtml(item.customer_id)}"` : ""}><span class="muted">Pool</span><strong>${escapeHtml(metadata.pool_name || item.pool_id || "—")}</strong></article>
        <article class="stat-card ${assignedTechId ? "is-clickable" : ""}" ${assignedTechId ? `data-tech-id="${escapeHtml(assignedTechId)}"` : ""}><span class="muted">Assigned Tech</span><strong>${escapeHtml(assignedTech)}</strong></article>
        <article class="stat-card ${recentTechId ? "is-clickable" : ""}" ${recentTechId ? `data-tech-id="${escapeHtml(recentTechId)}"` : ""}><span class="muted">Recent Tech</span><strong>${escapeHtml(recentTech)}</strong></article>
      </div>
      <div class="meta-stack">
        ${observedCost ? `<div class="meta-row"><span>Observed Cost</span><strong>${escapeHtml(observedCost)}</strong></div>` : ""}
        ${thresholdCost ? `<div class="meta-row"><span>Threshold</span><strong>${escapeHtml(thresholdCost)}</strong></div>` : ""}
        <div class="meta-row"><span>Last Detected</span><strong>${formatDateTime(item.last_detected_at)}</strong></div>
        <div class="meta-row"><span>Snoozed Until</span><strong>${formatDateTime(item.snoozed_until)}</strong></div>
      </div>
    </section>
    <section class="section-card">
      <h3>Related Chemistry</h3>
      ${renderAlertCharts(detail, "profile")}
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
        ${isFilterCleanAlert(item) ? `<button class="button button-secondary" id="alert-notify-customer-button">Notify Customer</button>` : ""}
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
  const notifyButton = document.getElementById("alert-notify-customer-button");
  if (notifyButton) {
    notifyButton.onclick = () => mutate(async () => {
      const assignedTo = document.getElementById("alert-reminder-assigned").value.trim();
      const dueAt = toUtcIso(document.getElementById("alert-reminder-due").value);
      const note = document.getElementById("alert-reminder-note").value.trim();
      const params = new URLSearchParams({
        actor: state.actor || "ui",
        assigned_to: assignedTo,
        due_at: dueAt,
        note,
      });
      const payload = await api(`/api/alerts/${item.id}/notify-customer?${params.toString()}`, { method: "POST", auth: true });
      await loadAlertProfile();
      showToast(filterCleanNotifyToast(payload), 4200);
    });
  }
  wireNavigationTargets(els.mainPanel);
}

async function loadCustomers(resetOffset = false) {
  if (resetOffset) filters.customers.offset = 0;
  const result = await api(`/api/customers${qs(filters.customers)}`);
  state.data.customers = result;
  renderCustomers();
}

function renderCustomers() {
  const result = state.data.customers;
  els.mainPanel.innerHTML = `
    <section class="section-card">
      <h3>Customer List</h3>
      <p class="panel-subtitle">${escapeHtml(result.total)} customers in scope. Showing 20 at a time. Click any row to open the full customer page.</p>
      <div class="item-list">
        ${result.items.map((item) => {
          const name = safeName(`${item.first_name || ""} ${item.last_name || ""}`.trim(), item.company_name, item.email, `Customer ${item.id}`);
          return `
            <article class="item-card is-clickable" data-customer-id="${item.id}">
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
      ${renderPaginationControls(result, "customers")}
    </section>
  `;
  wireNavigationTargets(els.mainPanel);
  wirePagination(els.mainPanel);
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
        <div class="detail-header">
          <h3>${escapeHtml(name)}</h3>
          <button class="button button-secondary" id="customer-open-profile">Open Full Profile</button>
        </div>
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
  const openProfileButton = document.getElementById("customer-open-profile");
  if (openProfileButton) {
    openProfileButton.onclick = () => {
      state.selections.customerChartDays = customerChartPolicy().default_days || DEFAULT_CUSTOMER_CHART_POLICY.default_days;
      state.selections.customerVisitsExpanded = false;
      setView("customer-profile");
    };
  }
  wireNavigationTargets(els.detailPanel);
}

function buildLineChart(seriesItem, options = {}) {
  const points = seriesItem.points || [];
  if (!points.length) {
    return `<div class="empty-state">No chart data.</div>`;
  }
  const compact = Boolean(options.compact);
  const width = compact ? 360 : 420;
  const height = compact ? 210 : 220;
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
  const bounds = chartBoundsForReading(readingKey, values, recommendedHigh);
  const paddedMin = bounds.min;
  const paddedMax = bounds.max;
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
    <svg class="chart-svg${compact ? " chart-svg-compact" : ""}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="${compact ? "xMinYMin meet" : "none"}" role="img" aria-label="Chemistry trend chart">
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
          <article class="item-card is-clickable" data-tech-id="${escapeHtml(item.tech_id)}">
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
  wireNavigationTargets(els.mainPanel);
}

async function loadLabor() {
  const result = await api(`/api/labor/payroll${qs(filters.labor)}`);
  state.data.labor = result;
  renderLabor();
}

function renderLabor() {
  const result = state.data.labor || {};
  const summary = result.summary || {};
  const rules = result.pay_rules || {};
  const items = result.items || [];
  const range = result.range || {};

  els.mainPanel.innerHTML = `
    <section class="section-card">
      <div class="item-card-header">
        <div>
          <h3>Payroll Snapshot</h3>
          <p class="panel-subtitle">${escapeHtml(range.label || "")} · Up to ${escapeHtml(rules.regular_pool_cap ?? 40)} pools count as regular Gusto hours, then the rest roll into commission.</p>
        </div>
        <div class="meta-stack">
          <div class="meta-row"><span>Pool Rate</span><strong>${escapeHtml(currency(rules.pool_rate) || "$0.00")}</strong></div>
          <div class="meta-row"><span>Filter Clean Rate</span><strong>${escapeHtml(currency(rules.filter_clean_rate) || "$0.00")}</strong></div>
        </div>
      </div>
      <div class="stat-grid">
        <article class="stat-card"><span class="muted">Payable Techs</span><strong>${escapeHtml(summary.payable_tech_count ?? 0)}</strong></article>
        <article class="stat-card"><span class="muted">Pools</span><strong>${escapeHtml(summary.total_pools ?? 0)}</strong></article>
        <article class="stat-card"><span class="muted">Regular Pools</span><strong>${escapeHtml(summary.total_regular_pools ?? 0)}</strong></article>
        <article class="stat-card"><span class="muted">Commission Pools</span><strong>${escapeHtml(summary.total_commission_pools ?? 0)}</strong></article>
        <article class="stat-card"><span class="muted">Filter Cleans</span><strong>${escapeHtml(summary.total_filter_cleans ?? 0)}</strong></article>
        <article class="stat-card"><span class="muted">Weekly Total</span><strong>${escapeHtml(currency(summary.total_pay) || "$0.00")}</strong></article>
      </div>
    </section>
    <section class="section-card">
      <div class="item-card-header">
        <div>
          <h3>Gusto Entry Sheet</h3>
          <p class="panel-subtitle">Regular hours = first ${escapeHtml(rules.regular_pool_cap ?? 40)} pools. Commission = over-40 pools plus filter-clean pay, combined into one Gusto commission amount.</p>
        </div>
      </div>
      <div class="payroll-table-wrap">
        <div class="payroll-table">
          <div class="payroll-table-row payroll-table-head">
            <div>Tech</div>
            <div>Pools</div>
            <div>Reg Hours</div>
            <div>Filter Cleans</div>
            <div>Comm Pools</div>
            <div>Regular</div>
            <div>Commission</div>
            <div>Total</div>
            <div>Notes</div>
          </div>
          ${items.map((item) => `
            <div class="payroll-table-row${item.is_salary ? " is-muted" : ""}">
              <div>
                <strong>${escapeHtml(item.tech_name || item.tech_id || "Unknown Tech")}</strong>
                <div class="muted">${escapeHtml(item.tech_id || "")}${item.service_day_count ? ` · ${escapeHtml(item.service_day_count)} service days` : ""}</div>
              </div>
              <div>${escapeHtml(item.pool_count ?? 0)}</div>
              <div>${escapeHtml(item.gusto_regular_hours ?? item.regular_pool_count ?? 0)}</div>
              <div>${escapeHtml(item.filter_clean_count ?? 0)}</div>
              <div>${escapeHtml(item.commission_pool_count ?? 0)}</div>
              <div>${escapeHtml(currency(item.regular_pool_pay) || "$0.00")}</div>
              <div>${escapeHtml(currency(item.gusto_commission_amount ?? item.commission_pool_pay) || "$0.00")}</div>
              <div><strong>${escapeHtml(currency(item.total_pay) || "$0.00")}</strong></div>
              <div>${escapeHtml(item.notes || "")}</div>
            </div>
          `).join("") || `<div class="empty-state">No labor activity found in this date range.</div>`}
        </div>
      </div>
    </section>
  `;
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
        <div class="detail-header">
          <h3>${escapeHtml(item.tech_name)}</h3>
          <button class="button button-secondary" id="technician-open-profile">Open Full Profile</button>
        </div>
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
  const openProfileButton = document.getElementById("technician-open-profile");
  if (openProfileButton) {
    openProfileButton.onclick = () => setView("technician-profile");
  }
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
    <div class="profile-grid technician-profile-grid">
      <div class="profile-main">
        <section class="section-card">
          <h3>Technician Snapshot</h3>
          <div class="meta-stack">
            <div class="meta-row"><span>Role</span><strong>${escapeHtml(item.role_type || "—")}</strong></div>
            <div class="meta-row"><span>Email</span><strong>${escapeHtml(item.email || "—")}</strong></div>
            <div class="meta-row"><span>Recent Route Activity</span><strong>${escapeHtml(item.route_stop_count_30d)}</strong></div>
            <div class="meta-row"><span>Earliest Start Time (30d)</span><strong>${formatClockTime(timing.earliest_route_start_30d)}</strong></div>
            <div class="meta-row"><span>Latest Start Time (30d)</span><strong>${formatClockTime(timing.latest_route_start_30d)}</strong></div>
          </div>
        </section>
        <section class="section-card">
          <div class="item-card-header">
            <h3>Alerts Assigned</h3>
            <span class="dense">${escapeHtml(alerts.length)} active</span>
          </div>
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
      </div>
      <aside class="profile-side">
        <section class="section-card">
          <h3>Weekly Schedule</h3>
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
      </aside>
    </div>
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

init().catch((error) => {
  setStatus("Error", "critical");
  els.mainPanel.innerHTML = `<div class="empty-state">Could not initialize the dashboard.<br /><br />${escapeHtml(error.message || String(error))}</div>`;
});
