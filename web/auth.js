// Google sign-in through Google Identity Services' token client (a small Google popup).
import { CLIENT_ID } from "./config.js";

// drive.file: Driveup can only see files and folders it created itself.
const SCOPE = "https://www.googleapis.com/auth/drive.file";

export class AuthError extends Error {}

let memo; // { token, expires }
let client;
let answer = () => {}; // resolves the sign-in currently waiting on Google

// The library loads with an async <script>, so wait for it.
async function gis() {
  for (let i = 0; !globalThis.google?.accounts?.oauth2; i++) {
    if (i > 100) throw new AuthError("Google sign-in didn't load. Check your connection and reload the page.");
    await new Promise((r) => setTimeout(r, 100));
  }
  return google.accounts.oauth2;
}

async function flow(interactive, hint) {
  if (!navigator.onLine) throw new TypeError("offline"); // let the uploader wait for the network
  const oauth2 = await gis();
  client ??= oauth2.initTokenClient({
    client_id: CLIENT_ID,
    scope: SCOPE,
    callback: (r) => answer(r),
    error_callback: (err) => answer({ error: err.type === "popup_failed_to_open" ? "popup_blocked" : err.type }),
  });
  const response = await new Promise((resolve) => {
    answer = resolve;
    // Renewing without a click only works if the browser allows this site's pop-ups.
    client.requestAccessToken({ prompt: interactive ? "select_account" : "none", login_hint: hint });
  });
  if (!response.access_token) {
    throw new AuthError(response.error === "popup_blocked" ? "Your browser blocked Google's sign-in window" : response.error || "Sign-in failed");
  }
  memo = { token: response.access_token, expires: Date.now() + Number(response.expires_in || 3600) * 1000 };
  sessionStorage.setItem("driveup-auth", JSON.stringify(memo));
  return memo.token;
}

// Tokens last an hour; renew ahead of time so long uploads don't stop.
export async function token(force = false) {
  memo ??= JSON.parse(sessionStorage.getItem("driveup-auth") || "null");
  if (!force && memo?.expires > Date.now() + 120_000) return memo.token;
  return flow(false, await account());
}

export async function signIn() {
  await flow(true);
  const r = await fetch("https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)", {
    headers: { Authorization: `Bearer ${memo.token}` },
  });
  const { user } = await r.json();
  localStorage.setItem("driveup-email", user.emailAddress);
  return user.emailAddress;
}

export async function account() {
  return localStorage.getItem("driveup-email");
}

export async function signOut() {
  if (memo?.token) globalThis.google?.accounts?.oauth2.revoke(memo.token, () => {});
  memo = null;
  sessionStorage.removeItem("driveup-auth");
  localStorage.removeItem("driveup-email");
}
