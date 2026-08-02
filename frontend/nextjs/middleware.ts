import { NextRequest, NextResponse } from "next/server";

import { TEAM_ACCESS_COOKIE, teamAccessState, verifyTeamAccessCookieValue } from "@/lib/teamAccess";

/**
 * Shared-password gate for the team UI.
 *
 * Everything is gated by default — pages and the /api/* backend proxies the
 * browser calls after load — except /login, the login POST, and static
 * assets (excluded in the matcher below). The research WebSocket connects
 * straight to the backend host, so it is untouched by this middleware; the
 * backend keeps its own API_AUTH_KEY / ws-token auth.
 *
 * Production fails closed if either auth variable is missing. Only local
 * development receives the explicit no-secret bypass.
 */
export async function middleware(request: NextRequest) {
  const access = teamAccessState();
  if (access.mode === "local-bypass") {
    return NextResponse.next();
  }

  const { pathname } = request.nextUrl;

  // Pre-auth surface: the login page and the login POST itself.
  if (pathname === "/login" || pathname === "/api/auth/login" || pathname === "/api/auth/logout") {
    return NextResponse.next();
  }

  if (access.mode === "misconfigured") {
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ error: access.reason }, { status: 503 });
    }
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = "/login";
    loginUrl.search = "?reason=configuration";
    return NextResponse.redirect(loginUrl);
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
  loginUrl.searchParams.set("reason", cookie ? "expired" : "required");
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
