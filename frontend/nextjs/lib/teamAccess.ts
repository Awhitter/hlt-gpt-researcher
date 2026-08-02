/**
 * Shared-password team gate helpers.
 *
 * The whole team shares one password (TEAM_ACCESS_PASSWORD). A successful
 * login sets an HMAC-signed, httpOnly cookie that the middleware verifies on
 * every request. Everything here uses Web Crypto so it runs identically in
 * the Edge middleware and in Node route handlers.
 *
 * Production fails closed unless both auth variables exist. Local development
 * keeps an explicit bypass so contributors can run the UI without secrets.
 */

export const TEAM_ACCESS_COOKIE = "mr_team_access";
export const TEAM_ACCESS_MAX_AGE_SECONDS = 30 * 24 * 60 * 60; // 30 days

const encoder = new TextEncoder();

export function teamAccessPassword(): string | null {
  return process.env.TEAM_ACCESS_PASSWORD || null;
}

export type TeamAccessState =
  | { mode: "enabled"; reason: null }
  | { mode: "local-bypass"; reason: null }
  | { mode: "misconfigured"; reason: string };

export function teamAccessState(): TeamAccessState {
  const password = teamAccessPassword();
  const secret = process.env.TEAM_ACCESS_COOKIE_SECRET || null;
  if (password && secret) return { mode: "enabled", reason: null };
  if (process.env.NODE_ENV !== "production") return { mode: "local-bypass", reason: null };
  const missing = [
    !password ? "TEAM_ACCESS_PASSWORD" : null,
    !secret ? "TEAM_ACCESS_COOKIE_SECRET" : null,
  ].filter(Boolean);
  return {
    mode: "misconfigured",
    reason: `Team access is unavailable because ${missing.join(" and ")} is missing.`,
  };
}

/**
 * Cookie-signing secret. Production never derives it from the password;
 * rotating the explicit value invalidates all existing sessions.
 */
function cookieSecret(): string | null {
  const explicit = process.env.TEAM_ACCESS_COOKIE_SECRET;
  if (explicit) return explicit;
  if (process.env.NODE_ENV === "production") return null;
  const password = teamAccessPassword();
  if (!password) return null;
  return `${password}::mastery-research-team-cookie`;
}

async function hmacHex(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(payload));
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Constant-time string equality via the double-HMAC trick: HMAC both values
 * with a random one-off key, then compare the MACs. The comparison of the
 * MACs may short-circuit, but the attacker cannot exploit it because the MAC
 * key is never revealed.
 */
export async function constantTimeEqual(a: string, b: string): Promise<boolean> {
  const oneOffKey = crypto.getRandomValues(new Uint8Array(32)).join("-");
  const [macA, macB] = await Promise.all([hmacHex(oneOffKey, a), hmacHex(oneOffKey, b)]);
  return macA === macB;
}

/** Create a signed cookie value: `<expiresEpochSeconds>.<hmac>`. */
export async function createTeamAccessCookieValue(): Promise<string | null> {
  const secret = cookieSecret();
  if (!secret) return null;
  const expires = String(Math.floor(Date.now() / 1000) + TEAM_ACCESS_MAX_AGE_SECONDS);
  const signature = await hmacHex(secret, expires);
  return `${expires}.${signature}`;
}

/** Verify a cookie value produced by createTeamAccessCookieValue. */
export async function verifyTeamAccessCookieValue(value: string | undefined | null): Promise<boolean> {
  const secret = cookieSecret();
  if (!secret || !value) return false;

  const dotIndex = value.indexOf(".");
  if (dotIndex <= 0) return false;
  const expiresText = value.slice(0, dotIndex);
  const providedSignature = value.slice(dotIndex + 1);

  const expires = Number(expiresText);
  if (!Number.isFinite(expires) || expires <= Math.floor(Date.now() / 1000)) {
    return false;
  }

  const expectedSignature = await hmacHex(secret, expiresText);
  return constantTimeEqual(providedSignature, expectedSignature);
}
