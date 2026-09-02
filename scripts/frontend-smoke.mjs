import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");
const frontendDir = path.join(repoRoot, "web-frontend");
const appJsPath = path.join(frontendDir, "app.js");
const cssPath = path.join(frontendDir, "styles.css");
const htmlPath = path.join(frontendDir, "index.html");
const caddyPath = path.join(frontendDir, "Caddyfile");

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function readUtf8(filePath) {
  return readFileSync(filePath, "utf8");
}

function verifyHtmlReferences(html) {
  assert(
    html.includes('<link rel="stylesheet" href="/styles.css" />'),
    "index.html must reference /styles.css",
  );
  assert(
    html.includes('<script src="/app.js"></script>'),
    "index.html must reference /app.js",
  );
  assert(
    html.includes('<img src="/ntpp-logo.png" alt="" />'),
    "the login page must display the NTPP logo asset",
  );
  assert(/id="main-panel"/.test(html), "index.html is missing #main-panel");
  assert(/id="detail-panel"/.test(html), "index.html is missing #detail-panel");
}

function verifyFrontendHooks(js) {
  const requiredFunctions = [
    "init",
    "renderHome",
    "renderAlerts",
    "renderCustomers",
    "renderProblemPools",
    "renderTechnicians",
    "renderLabor",
    "renderReminders",
    "renderSettings",
  ];
  requiredFunctions.forEach((name) => {
    assert(js.includes(`function ${name}(`), `app.js is missing ${name}()`);
  });
  assert(
    js.includes("function buildLineChart("),
    "app.js must define buildLineChart()",
  );
  assert(js.includes("const state = {"), "app.js must define state");
  assert(
    js.includes(
      'setView(state.auth.access?.landing_view || "home", { pushHistory: false })',
    ),
    "app.js must initialize the role-appropriate landing view",
  );
  assert(
    js.includes('return isTechnicianPortal() ? "route-rollover" : view;'),
    "app.js must keep technician accounts in the rollover portal",
  );
  assert(
    js.includes("function showSignInPage("),
    "app.js must provide a full sign-in-page transition",
  );
  assert(
    js.includes("if (response.status === 401)"),
    "all expired API sessions must return to the sign-in page",
  );
  assert(
    js.includes('document.addEventListener("visibilitychange"'),
    "the app must recheck a phone session when returning to the foreground",
  );
}

function verifyChartStyling(css) {
  const requiredSelectors = [
    ".chart-grid",
    ".chart-grid-wide",
    ".chart-grid-alert",
    ".chart-card",
    ".chart-card-alert",
    ".chart-svg",
    ".chart-caption",
  ];
  requiredSelectors.forEach((selector) => {
    assert(css.includes(selector), `styles.css is missing ${selector}`);
  });
}

function verifyCachePolicy(caddy) {
  assert(
    caddy.includes(
      'header Cache-Control "no-store, no-cache, must-revalidate"',
    ),
    "Caddyfile must disable caching for the homepage and SPA fallback routes",
  );
  assert(
    !caddy.includes("header /index.html Cache-Control"),
    "Caddyfile must not limit the HTML cache policy to /index.html",
  );
}

function main() {
  const html = readUtf8(htmlPath);
  const js = readUtf8(appJsPath);
  const css = readUtf8(cssPath);
  const caddy = readUtf8(caddyPath);

  verifyHtmlReferences(html);
  verifyFrontendHooks(js);
  verifyChartStyling(css);
  verifyCachePolicy(caddy);

  console.log("frontend smoke checks passed");
}

main();
