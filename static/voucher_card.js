/* Voucher card expand/collapse (templates/_vcard.html).
 *
 * Clicking a card's bill number toggles its installments panel (.vc-ext), which
 * lives inside the card frame. A panel carrying data-src is filled once, on
 * first open, from GET <src>?fragment=1 (live master data); one without it
 * already holds its installments. Modified clicks (open in new tab) and
 * no-JS browsers fall through to the link's href.
 */
(function () {
  function load(panel) {
    if (panel.dataset.loaded || panel.dataset.loading) return;
    panel.dataset.loading = "1";
    var src = panel.dataset.src;
    var sep = src.indexOf("?") === -1 ? "?" : "&";
    fetch(src + sep + "fragment=1", { credentials: "same-origin" })
      .then(function (resp) {
        if (resp.redirected) {
          // Session expired: the request bounced to the login page.
          window.location.href = resp.url;
          return null;
        }
        return resp.text().then(function (text) { return { ok: resp.ok, text: text }; });
      })
      .then(function (r) {
        if (!r) return;
        // The 404 body is our own "Voucher not found" snippet — show it too.
        if (!r.ok && !r.text) throw new Error("empty");
        panel.innerHTML = '<h4>Installments</h4>' + r.text;
        panel.dataset.loaded = "1";
      })
      .catch(function () {
        panel.innerHTML = '<p class="vc-empty">Could not load voucher details. Close and reopen to retry.</p>';
      })
      .then(function () { delete panel.dataset.loading; });
  }

  document.addEventListener("click", function (e) {
    var toggle = e.target.closest ? e.target.closest("[data-vc-toggle]") : null;
    if (!toggle) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
    var card = toggle.closest(".vc");
    var panel = card && card.querySelector(".vc-ext");
    if (!panel) return;
    e.preventDefault();
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden && panel.dataset.src) {
      if (panel.querySelector(".vc-empty") && !panel.dataset.loaded) panel.innerHTML = '<p class="vc-empty">Loading…</p>';
      load(panel);
    }
  });
})();
