window.__castApiAvailable = false;
window.__onGCastApiAvailable = function (isAvailable) {
  window.__castApiAvailable = !!isAvailable;
  if (isAvailable) {
    window.dispatchEvent(new Event("cast-api-available"));
  }
};
