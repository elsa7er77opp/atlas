/*
 * Project Atlas - API client
 * AT-26-L1-002 | dashboard
 *
 * Every request the dashboard makes goes through this file. It exists to solve
 * one problem the API creates deliberately: access tokens expire after fifteen
 * minutes. Without handling that, a user gets thrown back to the login screen
 * mid-task, four times an hour.
 *
 * Two decisions here are worth being able to defend in review.
 *
 * 1. SINGLE-FLIGHT REFRESH
 *    If five requests fail with 401 at the same moment, only ONE refresh may
 *    run. The API issues single-use refresh tokens and detects replay: a
 *    second refresh with an already-spent token is treated as theft and the
 *    whole token family is revoked. That behaviour is correct and is verified
 *    in verify_api.sh. So a naive implementation that refreshes per failed
 *    request would log the user out because a security control worked. The
 *    `refreshInFlight` promise below is what prevents that.
 *
 * 2. TOKEN STORAGE
 *    The access token is held in memory only. It dies with the tab, which is
 *    the intended lifetime for a fifteen-minute credential.
 *    The refresh token is in localStorage so a page reload does not force a
 *    new login. This is a deliberate trade-off, not an oversight: localStorage
 *    is readable by any script on the page, so a cross-site scripting flaw
 *    would expose it. The stronger arrangement is an httpOnly cookie the
 *    browser attaches automatically and JavaScript cannot read — but the API
 *    currently returns tokens in the response body, so that would require an
 *    API change. Recorded as a known limitation rather than left implicit.
 */

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

const REFRESH_KEY = "atlas.refresh_token";

let accessToken = null;
let refreshInFlight = null;

/* ------------------------------------------------------------------ tokens */

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_KEY);
}

function setTokens({ access_token, refresh_token }) {
  accessToken = access_token;
  if (refresh_token) localStorage.setItem(REFRESH_KEY, refresh_token);
}

export function clearTokens() {
  accessToken = null;
  localStorage.removeItem(REFRESH_KEY);
}

export function isAuthenticated() {
  return Boolean(accessToken || getRefreshToken());
}

/*
 * The role travels inside the access token as a claim. Reading it here lets
 * the interface hide controls the user cannot use.
 *
 * This is a USABILITY measure and nothing more. The authorisation decision is
 * made by the API on every request, from this same claim, and is verified
 * independently of the interface. Hiding a button does not protect an
 * endpoint; the endpoint protects the endpoint.
 */
export function getCurrentUser() {
  if (!accessToken) return null;
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return { id: payload.sub, username: payload.username, role: payload.role };
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------- auth */

async function publicAuthRequest(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    if (res.status === 409) throw new Error("This username or email is already in use.");
    let detail = `Request failed (${res.status}).`;
    try {
      const payload = await res.json();
      if (payload.detail) detail = typeof payload.detail === "string" ? payload.detail : detail;
    } catch {
      /* keep the status message */
    }
    throw new Error(detail);
  }
  return res.json();
}

export function registerAccount(username, email, password) {
  return publicAuthRequest("/api/auth/register", { username, email, password });
}

export function verifyEmail(email, code) {
  return publicAuthRequest("/api/auth/verify-email", { email, code });
}

export function resendVerification(email) {
  return publicAuthRequest("/api/auth/resend-verification", { email });
}

export function requestPasswordReset(email) {
  return publicAuthRequest("/api/auth/forgot-password", { email });
}

export function resetPassword(email, code, new_password) {
  return publicAuthRequest("/api/auth/reset-password", { email, code, new_password });
}

export async function login(username, password) {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

  if (!res.ok) {
    // The API returns an identical response for a wrong password and an
    // unknown account, so there is no account enumeration oracle. The
    // interface must not undo that by distinguishing them here.
    if (res.status === 401) throw new Error("Incorrect username or password.");
    if (res.status === 429) throw new Error("Too many attempts. Wait a moment and try again.");
    throw new Error(`Login failed (${res.status}).`);
  }

  setTokens(await res.json());
  return getCurrentUser();
}

export async function logout() {
  const refresh = getRefreshToken();
  if (refresh) {
    // Best-effort: the server-side revocation matters, but a failure here must
    // not prevent the client from discarding its own tokens.
    try {
      await fetch(`${API_BASE}/api/auth/logout`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        },
        body: JSON.stringify({ refresh_token: refresh }),
      });
    } catch {
      /* ignore */
    }
  }
  clearTokens();
}

/*
 * Exchange the refresh token for a new pair. Never call this directly from
 * more than one place at a time - see the single-flight note in the header.
 */
async function refreshTokens() {
  const refresh = getRefreshToken();
  if (!refresh) throw new Error("No refresh token.");

  const res = await fetch(`${API_BASE}/api/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });

  if (!res.ok) {
    clearTokens();
    throw new Error("Session expired.");
  }

  setTokens(await res.json());
}

/* --------------------------------------------------------------- requests */

async function rawRequest(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

/*
 * The single entry point for authenticated calls.
 *
 * On 401 it refreshes once and retries the original request. The
 * `refreshInFlight` promise is shared, so concurrent 401s wait on the same
 * refresh rather than each starting their own.
 */
export async function apiRequest(path, options = {}) {
  let res = await rawRequest(path, options);

  if (res.status === 401 && getRefreshToken()) {
    if (!refreshInFlight) {
      refreshInFlight = refreshTokens().finally(() => {
        refreshInFlight = null;
      });
    }

    try {
      await refreshInFlight;
    } catch {
      clearTokens();
      throw new Error("Session expired. Please sign in again.");
    }

    res = await rawRequest(path, options);
  }

  if (!res.ok) {
    // 403 is not a failure to authenticate - it is a successful
    // authentication followed by a refused authorisation. Saying so plainly
    // avoids the user retrying their password against a permissions problem.
    if (res.status === 403) {
      throw new Error("Your role does not permit this action.");
    }
    let detail = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      /* response was not JSON; keep the status message */
    }
    throw new Error(detail);
  }

  if (res.status === 204) return null;
  return res.json();
}

/* ----------------------------------------------------------------- restore */

/*
 * Called once when the application starts. A page reload discards the
 * in-memory access token, so this trades the stored refresh token for a fresh
 * pair before any screen tries to load data.
 */
export async function restoreSession() {
  if (accessToken) return getCurrentUser();
  if (!getRefreshToken()) return null;
  try {
    await refreshTokens();
    return getCurrentUser();
  } catch {
    return null;
  }
}

/*
 * The WebSocket needs the raw access token: the browser WebSocket API cannot
 * set an Authorization header, so the token travels as a query parameter and
 * the socket layer has to read it directly rather than going through
 * apiRequest. Exposed as a named function rather than exporting the variable
 * so the token stays module-private and read-only from outside.
 */
export function getAccessTokenForSocket() {
  return accessToken;
}
