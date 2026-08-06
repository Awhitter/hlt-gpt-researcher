import { NextResponse } from "next/server";
import { backendHeaders, backendUrl } from "../../_utils/backend";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${backendUrl()}/api/brain/suggestions`, {
      headers: backendHeaders(),
      cache: "no-store",
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    // The strip renders nothing on an empty bank, so a backend outage costs a
    // nicety rather than showing an error for something nobody asked for.
    return NextResponse.json(
      {
        suggestions: [],
        error: error instanceof Error ? error.message : "Backend unreachable",
      },
      { status: 502 },
    );
  }
}
