import { createContext, use, useEffect, useRef, useState } from "react";
import { ApiError, request } from "./api";
import type { Identity } from "./types";

type SessionContextValue = {
  identity: Identity | null | undefined;
  error: string;
  login: (token: string) => Promise<void>;
  logout: () => Promise<void>;
  mutate: <T>(url: string, body: Record<string, unknown>) => Promise<T>;
  restore: () => Promise<Identity | null>;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [identity, setIdentity] = useState<Identity | null | undefined>(undefined);
  const [error, setError] = useState("");
  const channelRef = useRef<BroadcastChannel | null>(null);

  const publish = (message: { type: "csrf"; identity: Identity } | { type: "logout" }) => {
    channelRef.current?.postMessage(message);
  };

  async function restore() {
    try {
      const current = await request<Identity>("/pg/session");
      setIdentity(current);
      setError("");
      publish({ type: "csrf", identity: current });
      return current;
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setIdentity(null);
        return null;
      }
      setError(reason instanceof Error ? reason.message : String(reason));
      setIdentity(null);
      return null;
    }
  }

  useEffect(() => {
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("mapa-v3-session");
    channelRef.current = channel;
    restore();
    if (!channel) return;
    channel.onmessage = (event: MessageEvent) => {
      if (event.data?.type === "logout") setIdentity(null);
      if (event.data?.type === "csrf" && event.data.identity) setIdentity(event.data.identity as Identity);
    };
    return () => {
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, []);

  async function login(token: string) {
    const current = await request<Identity>("/pg/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    setIdentity(current);
    setError("");
    publish({ type: "csrf", identity: current });
  }

  async function logout() {
    const csrf = identity?.csrf || "";
    try {
      await request<{ ok: boolean }>("/pg/session", {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrf },
      });
    } finally {
      setIdentity(null);
      publish({ type: "logout" });
    }
  }

  async function mutate<T>(url: string, body: Record<string, unknown>): Promise<T> {
    async function send(csrf: string) {
      return request<T>(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(body),
      }, 30_000);
    }
    try {
      return await send(identity?.csrf || "");
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.status !== 403 || !reason.message.includes("CSRF")) throw reason;
      const refreshed = await restore();
      if (!refreshed?.csrf) throw reason;
      return send(refreshed.csrf);
    }
  }

  return <SessionContext value={{ identity, error, login, logout, mutate, restore }}>{children}</SessionContext>;
}

export function useSession() {
  const context = use(SessionContext);
  if (!context) throw new Error("useSession requiere SessionProvider");
  return context;
}
