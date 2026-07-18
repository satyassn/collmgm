/* Inline voucher detail expand.
 *
 * Any <a class="voucher-link"> inside a table row toggles a detail row
 * beneath it, filled from GET <href>?fragment=1. Links outside a table,
 * modified clicks (new tab), and no-JS browsers fall back to normal
 * navigation to the full /voucher/{bill_no} page.
 */
(function () {
  document.addEventListener("click", function (e) {
    var link = e.target.closest ? e.target.closest("a.voucher-link") : null;
    if (!link) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
    var row = link.closest("tr");
    if (!row) return;
    e.preventDefault();

    var detail = row.nextElementSibling;
    if (detail && detail.classList.contains("voucher-detail-row")) {
      detail.hidden = !detail.hidden;
      link.setAttribute("aria-expanded", String(!detail.hidden));
      return;
    }

    detail = document.createElement("tr");
    detail.className = "voucher-detail-row";
    var cell = document.createElement("td");
    cell.colSpan = row.children.length;
    cell.textContent = "Loading…";
    detail.appendChild(cell);
    row.parentNode.insertBefore(detail, row.nextSibling);
    link.setAttribute("aria-expanded", "true");

    var sep = link.getAttribute("href").indexOf("?") === -1 ? "?" : "&";
    fetch(link.getAttribute("href") + sep + "fragment=1", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.redirected) {
          // Session expired: the request bounced to the login page. Navigate
          // there instead of injecting a full HTML document into the row.
          window.location.href = resp.url;
          return null;
        }
        return resp.text().then(function (text) {
          return { ok: resp.ok, text: text };
        });
      })
      .then(function (r) {
        if (!r) return;
        // The 404 body is our own "Voucher not found" snippet — show it too.
        cell.innerHTML = r.text || "";
        if (!r.ok && !r.text) cell.textContent = "Could not load voucher details.";
      })
      .catch(function () {
        cell.textContent = "Could not load voucher details.";
      });
  });
})();
