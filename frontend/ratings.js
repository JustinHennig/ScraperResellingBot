let records = [];

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
  const up = records.filter(r => r.rating === "up").length;
  const down = records.filter(r => r.rating === "down").length;
  document.getElementById("ratingsSummary").innerHTML = `
    <div class="ratings-stat"><span>${records.length}</span>Notified</div>
    <div class="ratings-stat up"><span>${up}</span>👍</div>
    <div class="ratings-stat down"><span>${down}</span>👎</div>
    <div class="ratings-stat"><span>${records.length - up - down}</span>Unrated</div>`;
}

// Fills the search dropdown with every search name that has a record.
function renderSearchOptions() {
  const select = document.getElementById("searchFilter");
  const names = [...new Set(records.map(r => r.search_name).filter(Boolean))].sort();
  select.innerHTML = `<option value="">All</option>` +
    names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
}

// Renders the table rows matching the current filters.
function renderTable() {
  const ratingFilter = document.getElementById("ratingFilter").value;
  const searchFilter = document.getElementById("searchFilter").value;

  const rows = records.filter(r => {
    if (searchFilter && r.search_name !== searchFilter) return false;
    if (ratingFilter === "rated") return r.rating !== null;
    if (ratingFilter === "none") return r.rating === null;
    if (ratingFilter === "up" || ratingFilter === "down") return r.rating === ratingFilter;
    return true;
  });

  document.getElementById("ratingsBody").innerHTML = rows.map(r => {
    const icon = r.rating === "up" ? "👍" : r.rating === "down" ? "👎" : "–";
    const price = r.price != null ? `${r.price} €` : "";
    return `<tr>
      <td class="rating-icon">${icon}</td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a></td>
      <td class="muted">${esc(r.search_name)}</td>
      <td class="num">${price}</td>
      <td class="muted">${fmtDate(r.notified_at)}</td>
    </tr>`;
  }).join("");
  document.getElementById("ratingsEmpty").hidden = rows.length > 0;
}

// Loads all records from the server and re-renders the page.
async function fetchRatings() {
  try {
    const res = await fetch("/api/ratings");
    const data = await res.json();
    records = data.records;
    renderSummary();
    renderSearchOptions();
    renderTable();
  } catch (_) {}
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ratingFilter").addEventListener("change", renderTable);
  document.getElementById("searchFilter").addEventListener("change", renderTable);
  fetchRatings();
});
