const LOGOUT_MARKER = "edream_explicit_logout";

export function isExplicitlyLoggedOut() {
  try {
    return window.sessionStorage.getItem(LOGOUT_MARKER) === "1";
  } catch {
    return false;
  }
}

export function beginLogin(next = window.location.pathname) {
  try {
    window.sessionStorage.removeItem(LOGOUT_MARKER);
  } catch {
    /* Storage may be unavailable in a restricted browser context. */
  }
  window.location.href = `/api/auth/login?next=${encodeURIComponent(next)}`;
}

export function completeLogout(ssoLogoutUrl: string | null) {
  try {
    window.sessionStorage.setItem(LOGOUT_MARKER, "1");
  } catch {
    /* The in-memory logged-out state still keeps the current page stable. */
  }
  if (!ssoLogoutUrl) return;
  void fetch(ssoLogoutUrl, {
    method: "POST",
    mode: "no-cors",
    credentials: "include",
    keepalive: true,
  }).catch(() => {
    /* Local logout has already succeeded; SSO cleanup is best effort. */
  });
}
