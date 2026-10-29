import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../components/auth";
import { useToast } from "../components/Toasts";
import { ErrorNote, Field, PageHeader } from "../components/ui";

/** The signed-in user's own account: who they are, change password, sign out. */
export default function AccountPage() {
  const { me, user, signOut } = useAuth();
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const change = useMutation({
    mutationFn: () => api.changePassword(cur, pw),
    onSuccess: () => { setCur(""); setPw(""); setPw2(""); toast.push({ title: "Password changed", description: "Your other sessions were signed out.", tone: "ok" }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); change.mutate(); };
  const mismatch = pw2 !== "" && pw !== pw2;

  return (
    <>
      <PageHeader title="My account" actions={me.auth && <button onClick={signOut}><LogOut size={15} />Sign out</button>} />
      <section className="card">
        <dl className="logdetail kv" style={{ margin: 0, border: 0, padding: 0, background: "none" }}>
          <dt>Username</dt><dd className="mono">{user.username}</dd>
          {user.display_name && <><dt>Name</dt><dd>{user.display_name}</dd></>}
          <dt>Role</dt><dd>{user.role === "admin" ? "Admin (manages users and settings)" : "User"}</dd>
        </dl>
        {!me.auth && <p className="muted small" style={{ marginBottom: 0 }}>This server runs without sign-in (<span className="mono">--no-auth</span>), so you are the local admin.</p>}
      </section>
      {me.auth && (
        <form className="card" onSubmit={submit}>
          <h2>Change password</h2>
          <div className="fields">
            <Field label="Current password"><input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required /></Field>
            <Field label="New password" hint="At least 8 characters"><input type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} required /></Field>
            <Field label="Repeat new password" hint={mismatch ? "Passwords do not match" : undefined}><input type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} required aria-invalid={mismatch} /></Field>
          </div>
          <ErrorNote error={change.error} />
          <div className="row end" style={{ marginTop: 14 }}>
            <button className="primary" type="submit" disabled={change.isPending || !cur || pw.length < 8 || pw !== pw2}>{change.isPending && <span className="spinner" />}Change password</button>
          </div>
        </form>
      )}
    </>
  );
}
