import { NextRequest, NextResponse } from "next/server";

import { TEAM_ACCESS_COOKIE, teamAccessPassword, verifyTeamAccessCookieValue } from "@/lib/teamAccess";

/**
 * Shared-password gate for the team UI.
 *
 * Everything is gated by default — pages and the /api/* backend proxies the
 * browser calls after load — except /login, the login POST, and static
 * assets (excluded in the matcher below). The research WebSocket connects
 * straight to the backend host, so it is untouched by this middleware; the
 * backend keeps its own API_AUTH_KEY / ws-token auth.
 *
 * With TEAM_ACCESS_PASSWORD unset the gate is disabled (local dev parity
 * with the backend's API_AUTH_KEY behavior).
 */
export async function middleware(request: NextRequest) {
  if (!teamAccessPassword()) {
    return NextResponse.next();
  }

  const { pathname } = request.nextUrl;

  // Pre-auth surface: the login page and the login POST itself.
  if (pathname === "/login" || pathname === "/api/auth/login") {
    return NextResponse.next();
  }

  const cookie = request.cookies.get(TEAM_ACCESS_COOKIE)?.value;
  if (await verifyTeamAccessCookieValue(cookie)) {
    return NextResponse.next();
  }

  // API calls get a JSON 401; page loads get sent to /login.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "Team access required" }, { status: 401 });
  }

  const loginUrl = request.nextUrl.clone();
  loginUrl.pathname = "/login";
  loginUrl.search = "";
  if (pathname !== "/") {
    loginUrl.searchParams.set("from", pathname);
  }
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: [
    /*
     * Gate everything except:
     * - _next internals (static chunks, image optimizer)
     * - PWA plumbing (sw.js, workbox chunks, manifest.json)
     * - static assets (favicon, /img, embed.js, svg/png/ico files)
     */
    "/((?!_next/static|_next/image|favicon\\.ico|manifest\\.json|sw\\.js|workbox-.*\\.js|embed\\.js|img/|.*\\.svg$|.*\\.png$|.*\\.ico$).*)",
  ],
};
