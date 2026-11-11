import type { components } from "./api-types";

export type HierarchyNode = components["schemas"]["HierarchyNode"];
export type TimeSeries = components["schemas"]["TimeSeries"];
export type BucketTotals = components["schemas"]["BucketTotals"];
export type Crumb = components["schemas"]["Crumb"];
export type Summary = components["schemas"]["Summary"];
export type Level = HierarchyNode["level"];
export type Bucket = TimeSeries["bucket"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | undefined>;

async function request<T>(path: string, init?: RequestInit & { params?: Params }): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(init?.params ?? {})) {
    if (value) query.set(key, value);
  }
  const url = query.size ? `${path}?${query}` : path;
  const response = await fetch(url, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export interface Range {
  from?: string;
  to?: string;
}

export const api = {
  me: () => request<{ user_id: number }>("/api/me"),
  login: (token: string) =>
    request<{ user_id: number }>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  hierarchy: (range: Range) => request<HierarchyNode>("/api/hierarchy", { params: { ...range } }),
  timeseries: (level: Level, id: string, bucket: Bucket, range: Range) =>
    request<TimeSeries>("/api/timeseries", { params: { level, id, bucket, ...range } }),
};
