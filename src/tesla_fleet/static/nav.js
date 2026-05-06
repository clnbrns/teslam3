// Touch-friendly nav: convert :hover dropdowns into click-to-toggle.
// Hover still works on desktop (CSS keeps :hover/:focus-within rules);
// this script just adds a click handler so the same UI works on touch.
(function () {
  function init() {
    document.querySelectorAll('.nav-group').forEach(function (group) {
      var trigger = group.querySelector('a');
      if (!trigger) return;
      trigger.addEventListener('click', function (e) {
        // Allow direct nav on hover-capable devices
        if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
        // On touch: first tap opens menu, second tap or external tap closes it
        if (!group.classList.contains('open')) {
          e.preventDefault();
          document.querySelectorAll('.nav-group.open').forEach(function (g) { g.classList.remove('open'); });
          group.classList.add('open');
        }
      });
    });
    document.addEventListener('click', function (e) {
      if (e.target.closest('.nav-group')) return;
      document.querySelectorAll('.nav-group.open').forEach(function (g) { g.classList.remove('open'); });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else { init(); }
})();
