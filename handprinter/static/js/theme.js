// Applies the saved Appearance choice before the page paints (no flash). Classic script, not a module.
(function () {
  try {
    var t = localStorage.getItem('hp-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: follow the device */ }
})();
