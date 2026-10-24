(function () {
  var select = document.getElementById("cadence-select");
  if (!select) return;

  function sync() {
    var monthly = select.value === "monthly";
    document.querySelectorAll('[data-cadence-field="week"]').forEach(function (el) {
      el.hidden = !monthly;
    });
  }

  select.addEventListener("change", sync);
  sync();
})();
