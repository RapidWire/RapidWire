// Landing page: fills the live counts of currencies, executions and transfers; the section stays hidden when none can be read.
import { api, countText } from "/assets/app.js";

async function loadStats() {
  const section = document.getElementById("live-stats");
  const fills = [
    api("/v1/currencies").then((body) => ["stat-currencies", body.items.length.toLocaleString("en-US")]),
    api("/v1/executions", { query: { per_page: 1 } }).then((page) => ["stat-executions", countText(page)]),
    api("/v1/history", { query: { per_page: 1 } }).then((page) => ["stat-transfers", countText(page)]),
  ];
  for (const result of await Promise.allSettled(fills)) {
    if (result.status === "fulfilled") {
      const [id, text] = result.value;
      document.getElementById(id).textContent = text;
      section.hidden = false;
    }
  }
}

loadStats();
