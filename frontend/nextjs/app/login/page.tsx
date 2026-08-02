"use client";

import { FormEvent, Suspense, useState } from "react";
import Image from "next/image";
import { useRouter, useSearchParams } from "next/navigation";

import { hltBranding } from "@/lib/hltBranding";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const reason = searchParams.get("reason");

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || submitting) return;
    setSubmitting(true);
    setError(null);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });

      if (response.ok) {
        const from = searchParams.get("from");
        // Only follow same-site relative paths; anything else goes home.
        const destination = from && from.startsWith("/") && !from.startsWith("//") ? from : "/";
        router.replace(destination);
        router.refresh();
        return;
      }

      if (response.status === 401) {
        setError("That password is not right.");
      } else if (response.status === 503) {
        setError("Team access is temporarily unavailable. The owner needs to restore the production access settings.");
      } else {
        setError("Something went wrong. Try again.");
      }
    } catch {
      setError("Something went wrong. Try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main
      className="flex min-h-screen items-center justify-center px-4"
      style={{ backgroundColor: hltBranding.deepNavy }}
    >
      <div
        className="w-full max-w-sm rounded-2xl p-8 shadow-2xl"
        style={{ backgroundColor: hltBranding.warmWhite }}
      >
        <div className="mb-6 flex items-center gap-3">
          <Image src={hltBranding.icon} alt="" width={36} height={36} />
          <div>
            <h1 className="text-lg font-semibold" style={{ color: hltBranding.deepNavy }}>
              {hltBranding.productName}
            </h1>
            <p className="text-xs text-gray-500">{hltBranding.ownerName} team workspace</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {reason === "expired" && (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">
              Your session expired or access was rotated. Enter the team password again.
            </p>
          )}
          {reason === "configuration" && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800">
              Team access is temporarily unavailable because the production login is not fully configured.
            </p>
          )}
          <label className="block">
            <span className="mb-1 block text-sm font-medium" style={{ color: hltBranding.deepNavy }}>
              Team password
            </span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoFocus
              autoComplete="current-password"
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition focus:border-transparent focus:ring-2"
              style={{ "--tw-ring-color": hltBranding.accent } as React.CSSProperties}
            />
          </label>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <button
            type="submit"
            disabled={!password || submitting}
            className="w-full rounded-lg px-3 py-2 text-sm font-semibold text-white transition disabled:opacity-50"
            style={{ backgroundColor: hltBranding.accent }}
          >
            {submitting ? "Checking…" : "Enter"}
          </button>
        </form>

        <p className="mt-6 text-center text-xs text-gray-400">
          Shared HLT team access · sessions last 30 days
        </p>
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
