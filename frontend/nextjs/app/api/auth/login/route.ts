import { NextRequest, NextResponse } from "next/server";

import {
  TEAM_ACCESS_COOKIE,
  TEAM_ACCESS_MAX_AGE_SECONDS,
  constantTimeEqual,
  createTeamAccessCookieValue,
  teamAccessPassword,
} from "@/lib/teamAccess";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const expected = teamAccessPassword();
  if (!expected) {
    // Gate disabled — nothing to log into.
    return NextResponse.json({ error: "Team access gate is not configured" }, { status: 404 });
  }

  let provided = "";
  try {
    const body = await request.json();
    provided = typeof body?.password === "string" ? body.password : "";
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  if (!provided || !(await constantTimeEqual(provided, expected))) {
    return NextResponse.json({ error: "Wrong password" }, { status: 401 });
  }

  const cookieValue = await createTeamAccessCookieValue();
  if (!cookieValue) {
    return NextResponse.json({ error: "Team access gate is not configured" }, { status: 500 });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set({
    name: TEAM_ACCESS_COOKIE,
    value: cookieValue,
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: TEAM_ACCESS_MAX_AGE_SECONDS,
  });
  return response;
}
