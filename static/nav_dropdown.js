document.addEventListener('click', function (e) {
  var toggle = e.target.closest('.nav-dropdown-toggle');
  document.querySelectorAll('.nav-dropdown-menu.open').forEach(function (m) {
    if (!toggle || m !== toggle.nextElementSibling) m.classList.remove('open');
  });
  if (toggle) toggle.nextElementSibling.classList.toggle('open');
});
