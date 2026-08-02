import { NextRequest, NextResponse } from "next/server";
import { backendHeaders, backendUrl } from "../../_utils/backend";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const response = await fetch(`${backendUrl()}/api/brain/change-request`, {
      method: "POST",
      headers: { ...backendHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "Backend unreachable" },
      { status: 502 },
    );
  }
}
