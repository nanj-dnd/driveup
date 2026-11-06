// Google sign-in through chrome.identity.launchWebAuthFlow (works in Chrome, Edge, Brave, Arc).
import { CLIENT_ID } from "./config.js";

// drive.file: Driveup can only see files and folders it created itself.
const SCOPE = "https://www.googleapis.com/auth/drive.file";

export class AuthError extends Error {}

let memo; // { token, expires }

async function flow(interactive, hint) {
  if (!navigator.onLine) throw new TypeError("offline"); // let the uploader wait for the network
  const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  url.search = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "token",
    redirect_uri: chrome.identity.getRedirectURL(),
    scope: SCOPE,
    prompt: interactive ? "select_account" : "none",
    ...(hint ? { login_hint: hint } : {}),
  });
  let redirect;
  try {
    redirect = await chrome.identity.launchWebAuthFlow({
      url: url.href, interactive, abortOnLoadForNonInteractive: false, timeoutMsForNonInteractive: 20000,
    });
  } catch (err) {
    throw new AuthError(err.message);
  }
  const params = new URLSearchParams(new URL(redirect).hash.slice(1));
  if (!params.get("access_token")) throw new AuthError(params.get("error") || "Sign-in failed");
  memo = { token: params.get("access_token"), expires: Date.now() + Number(params.get("expires_in") || 3600) * 1000 };
  await chrome.storage.session.set({ auth: memo });
  return memo.token;
}

// Tokens last an hour; renew silently so long uploads don't stop.
export async function token(force = false) {
  memo ??= (await chrome.storage.session.get("auth")).auth;
  if (!force && memo?.expires > Date.now() + 120_000) return memo.token;
  return flow(false, await account());
}

export async function signIn() {
  await flow(true);
  const r = await fetch("https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)", {
    headers: { Authorization: `Bearer ${memo.token}` },
  });
  const { user } = await r.json();
  await chrome.storage.local.set({ email: user.emailAddress });
  return user.emailAddress;
}

export async function account() {
  return (await chrome.storage.local.get("email")).email;
}

export async function signOut() {
  if (memo?.token) fetch(`https://oauth2.googleapis.com/revoke?token=${memo.token}`, { method: "POST" }).catch(() => {});
  memo = null;
  await chrome.storage.session.remove("auth");
  await chrome.storage.local.remove("email");
}
