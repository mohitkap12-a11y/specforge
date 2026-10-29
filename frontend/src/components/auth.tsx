import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, UNAUTHORIZED } from "../api";
import type { AppUser, Me } from "../types";
import LoginPage from "../pages/Login";

/** `canManageUsers`: an admin on a server with sign-in (without sign-in there are no accounts to manage). */
interface AuthCtx { me: Me; user: AppUser; isAdmin: boolean; canManageUsers: boolean; signOut: () => Promise<void> }
const Ctx = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth outside <AuthGate>");
  return c;
}

/** Renders the app only for a signed-in user (or when the server runs without sign-in); otherwise the sign-in page.
 *  Any API call answering 401 re-checks the session, so an expired or revoked session lands back here. */
export function AuthGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 30_000, refetchInterval: 60_000 });
  useEffect(() => {
    const on = () => { qc.invalidateQueries({ queryKey: ["me"] }); };
    window.addEventListener(UNAUTHORIZED, on);
    return () => window.removeEventListener(UNAUTHORIZED, on);
  }, [qc]);

  if (q.isLoading) return <div className="auth-wrap"><div className="row muted" role="status"><span className="spinner" /> Loading…</div></div>;
  if (!q.data) return <div className="auth-wrap"><div className="toast bad" role="alert">Cannot reach the SpecForge server. {q.error instanceof Error ? q.error.message : ""}</div></div>;
  const me = q.data;
  // resetQueries drops every cached page (of the previous user) and refetches the active ones, "me" included
  if (!me.user) return <LoginPage onSignedIn={() => qc.resetQueries()} />;
  const signOut = async () => {
    try { await api.logout(); } finally { await qc.resetQueries(); }
  };
  return <Ctx.Provider value={{ me, user: me.user, isAdmin: me.user.role === "admin", canManageUsers: me.auth && me.user.role === "admin", signOut }}>{children}</Ctx.Provider>;
}
