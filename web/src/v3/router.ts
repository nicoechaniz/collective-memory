import { useSyncExternalStore } from "react";

export type AtlasRoute = "atlas" | "buscar" | "archivo";
export type LabRoute = "descubrir" | "bandeja" | "evidencia" | "director";
export type Route = AtlasRoute | LabRoute;

function subscribe(callback: () => void) {
  window.addEventListener("popstate", callback);
  return () => window.removeEventListener("popstate", callback);
}

function snapshot() {
  return `${window.location.pathname}${window.location.search}`;
}

export function useLocationKey() {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export function routeFor(mode: "atlas" | "lab"): Route {
  const base = mode === "atlas" ? "/atlas-v3" : "/lab-v3";
  const tail = window.location.pathname.slice(base.length).replace(/^\/+|\/+$/g, "");
  const allowed = mode === "atlas"
    ? new Set<AtlasRoute>(["atlas", "buscar", "archivo"])
    : new Set<LabRoute>(["descubrir", "bandeja", "evidencia", "director"]);
  const fallback = mode === "atlas" ? "atlas" : "descubrir";
  return allowed.has(tail as never) ? tail as Route : fallback;
}

export function navigate(mode: "atlas" | "lab", route: Route, params?: URLSearchParams, replace = false) {
  const base = mode === "atlas" ? "/atlas-v3" : "/lab-v3";
  const path = route === "atlas" ? `${base}/` : `${base}/${route}`;
  const query = params?.toString();
  window.history[replace ? "replaceState" : "pushState"]({}, "", query ? `${path}?${query}` : path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function queryParams(): URLSearchParams {
  return new URLSearchParams(window.location.search);
}

export function updateQuery(name: string, value: string | null, replace = false) {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set(name, value);
  else url.searchParams.delete(name);
  window.history[replace ? "replaceState" : "pushState"]({}, "", `${url.pathname}${url.search}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
