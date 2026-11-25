let entries = [];

// Escapes text for safe insertion into HTML.
function esc(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Formats an ISO timestamp as a short local date/time.
function fmtDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// Renders the counts at the top of the page.
function renderSummary() {
  const done = entries.filter(e => e.done).length;
  const withListing = entries.filter(e => e.listing && e.listing.listing_id).length;
  document.getElementById("feedbackSummary").innerHTML = `
    <div class="ratings-stat"><span>${entries.length - done}</span>Offen</div>
    <div class="ratings-stat up"><span>${done}</span>Erledigt</div>
    <div class="ratings-stat"><span>${withListing}</span>Mit Anzeige</div>`;
}

// Renders the listing a suggestion was a reply to.
function renderListing(l) {
  if (!l) return "";
  if (!l.listing_id) {
    return l.message ? `<details class="feedback-details"><summary>Antwort auf Nachricht</summary><pre>${esc(l.message)}</pre></details>` : "";
  }
  const price = l.price != null ? ` · ${l.price} €` : "";
  const search = l.search_name ? ` · ${esc(l.search_name)}` : "";
  return `
    <div class="feedback-listing">
      <a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>
      <span class="muted">${price}${search}</span>
    </div>
    ${l.description ? `<details class="feedback-details"><summary>Beschreibung</summary><pre>${esc(l.description)}</pre></details>` : ""}
    ${l.message ? `<details class="feedback-details"><summary>Meldung</summary><pre>${esc(l.message)}</pre></details>` : ""}`;
}

// Renders the suggestions matching the current filter.
function renderList() {
  const filter = document.getElementById("doneFilter").value;
  const rows = entries.filter(e => filter === "all" || (filter === "done") === Boolean(e.done));
  document.getElementById("feedbackList").innerHTML = rows.map(e => `
    <div class="feedback-item${e.done ? " done" : ""}">
      <div class="feedback-head">
        <span class="muted">${fmtDate(e.created_at)}</span>
        <label class="feedback-done">
          <input type="checkbox" data-id="${esc(e.id)}" ${e.done ? "checked" : ""} /> erledigt
        </label>
      </div>
      <p class="feedback-text">${esc(e.text)}</p>
      ${renderListing(e.listing)}
    </div>`).join("");
  document.getElementById("feedbackEmpty").hidden = rows.length > 0;
}

// Saves the done checkbox of one suggestion.
async function toggleDone(event) {
  const box = event.target;
  if (!box.matches("input[type=checkbox][data-id]")) return;
  try {
    const res = await fetch("/api/feedback/done", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: box.dataset.id, done: box.checked }),
    });
    if (!res.ok) throw new Error();
    const entry = entries.find(e => e.id === box.dataset.id);
    if (entry) entry.done = box.checked;
    renderSummary();
    renderList();
  } catch (_) {
    box.checked = !box.checked;
  }
}

// Loads all suggestions from the server and re-renders the page.
async function fetchFeedback() {
  try {
    const res = await fetch("/api/feedback");
    entries = (await res.json()).entries;
    renderSummary();
    renderList();
  } catch (_) {}
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("doneFilter").addEventListener("change", renderList);
  document.getElementById("feedbackList").addEventListener("change", toggleDone);
  fetchFeedback();
});
