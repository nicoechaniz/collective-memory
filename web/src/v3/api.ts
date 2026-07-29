import type { Envelope } from "./types";

export class ApiError extends Error {
  status: number;
  payload: Record<string, unknown>;

  constructor(status: number, payload: Record<string, unknown>) {
    super(String(payload.error || `HTTP ${status}`));
    this.status = status;
    this.payload = payload;
  }
}

export async function request<T>(url: string, init: RequestInit = {}, timeout = 20_000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { credentials: "same-origin", ...init, signal: controller.signal });
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok) throw new ApiError(response.status, data as Record<string, unknown>);
    return data as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export function v2<T>(path: string): Promise<Envelope<T>> {
  return request<Envelope<T>>(`/ui/v2${path}`);
}

export function serviceLink(service: "atlas" | "lab", path: string): string {
  const configured = service === "atlas"
    ? import.meta.env.VITE_ATLAS_ORIGIN
    : import.meta.env.VITE_LAB_ORIGIN;
  if (configured) return `${configured.replace(/\/$/, "")}${path}`;
  const port = service === "atlas" ? 8899 : 8898;
  return `${window.location.protocol}//${window.location.hostname}:${port}${path}`;
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    notation: value > 9999 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatDate(value: string | number | null | undefined, includeTime = false): string {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("es-AR", includeTime
    ? { dateStyle: "short", timeStyle: "short" }
    : { dateStyle: "medium" }).format(date);
}

export function safeJson<T>(value: string, fallback: T): T {
  try {
    return JSON.parse(value || "") as T;
  } catch {
    return fallback;
  }
}
