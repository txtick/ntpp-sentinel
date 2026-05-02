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
    js.includes('setView("home", { pushHistory: false })'),
    "app.js must initialize the home view",
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

function main() {
  const html = readUtf8(htmlPath);
  const js = readUtf8(appJsPath);
  const css = readUtf8(cssPath);

  verifyHtmlReferences(html);
  verifyFrontendHooks(js);
  verifyChartStyling(css);

  console.log("frontend smoke checks passed");
}

main();
