import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, KeyRound, Trash2, UserPlus, Users as UsersIcon, X } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../components/auth";
import { useToast } from "../components/Toasts";
import { Empty, ErrorNote, Field, Loading, PageHeader } from "../components/ui";
import type { AppUser, UserRole } from "../types";

const STATUS: Record<string, { label: string; tone: string }> = {
  active: { label: "Active", tone: "ok" }, pending: { label: "Pending", tone: "info" },
  disabled: { label: "Disabled", tone: "warn" }, rejected: { label: "Rejected", tone: "bad" },
};
const when = (ts?: string | null) => (ts ? new Date(ts.replace(" ", "T") + "Z").toLocaleString() : "–");
const who = (u: AppUser) => <><strong>{u.display_name || u.username}</strong>{u.display_name && <div className="muted small mono">{u.username}</div>}</>;

function AddUser({ onDone }: { onDone: () => void }) {
  const [b, setB] = useState({ username: "", display_name: "", password: "", role: "user" as UserRole });
  const qc = useQueryClient();
  const toast = useToast();
  const m = useMutation({
    mutationFn: () => api.createUser({ ...b, username: b.username.trim(), display_name: b.display_name.trim() || undefined }),
    onSuccess: (u) => { qc.invalidateQueries({ queryKey: ["users"] }); toast.push({ title: `Added ${u.username}`, tone: "ok" }); onDone(); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); m.mutate(); };
  return (
    <form className="card" onSubmit={submit}>
      <h2>Add a user</h2>
      <p className="muted">The account is active straight away. Give the user their password; they can change it under <em>My account</em>.</p>
      <div className="fields">
        <Field label="Username" hint="3 to 32 characters: letters, digits, . _ -"><input autoFocus autoComplete="off" value={b.username} onChange={(e) => setB({ ...b, username: e.target.value })} required /></Field>
        <Field label="Full name (optional)"><input autoComplete="off" value={b.display_name} onChange={(e) => setB({ ...b, display_name: e.target.value })} /></Field>
        <Field label="Initial password" hint="At least 8 characters"><input type="password" autoComplete="new-password" value={b.password} onChange={(e) => setB({ ...b, password: e.target.value })} required /></Field>
        <Field label="Role">
          <select value={b.role} onChange={(e) => setB({ ...b, role: e.target.value as UserRole })}><option value="user">User</option><option value="admin">Admin</option></select>
        </Field>
      </div>
      <ErrorNote error={m.error} />
      <div className="row end" style={{ marginTop: 14 }}>
        <button type="button" className="ghost" onClick={onDone}>Cancel</button>
        <button className="primary" type="submit" disabled={m.isPending || !b.username.trim() || b.password.length < 8}>{m.isPending && <span className="spinner" />}Add user</button>
      </div>
    </form>
  );
}

function ResetPassword({ user, onDone }: { user: AppUser; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const toast = useToast();
  const m = useMutation({
    mutationFn: () => api.resetUserPassword(user.id, pw),
    onSuccess: () => { toast.push({ title: `New password set for ${user.username}`, description: "They were signed out everywhere; give them the new password.", tone: "ok" }); onDone(); },
  });
  return (
    <form className="row" onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
      <input type="password" autoFocus autoComplete="new-password" placeholder={`New password for ${user.username}`} aria-label={`New password for ${user.username}`}
        value={pw} onChange={(e) => setPw(e.target.value)} style={{ width: 260 }} />
      <button className="sm primary" type="submit" disabled={pw.length < 8 || m.isPending}>{m.isPending && <span className="spinner" />}Set password</button>
      <button className="sm ghost" type="button" onClick={onDone}>Cancel</button>
      {pw !== "" && pw.length < 8 && <span className="muted small">At least 8 characters</span>}
      <ErrorNote error={m.error} />
    </form>
  );
}

/** Admin only: approve access requests, add users, change roles, disable accounts, reset passwords. */
export default function UsersPage() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["users"], queryFn: api.users });
  const [adding, setAdding] = useState(false);
  const [resetFor, setResetFor] = useState<number | null>(null);
  const done = () => { qc.invalidateQueries({ queryKey: ["users"] }); qc.invalidateQueries({ queryKey: ["me"] }); };
  const update = useMutation({
    mutationFn: ({ u, b }: { u: AppUser; b: { role?: string; status?: string }; msg: string }) => api.updateUser(u.id, b),
    onSuccess: (_r, v) => { done(); toast.push({ title: v.msg, tone: "ok" }); },
    onError: (e) => toast.push({ title: "Not changed", description: e instanceof Error ? e.message : String(e), tone: "bad" }),
  });
  const remove = useMutation({
    mutationFn: (u: AppUser) => api.deleteUser(u.id),
    onSuccess: (_r, u) => { done(); toast.push({ title: `Deleted ${u.username}`, tone: "ok" }); },
    onError: (e) => toast.push({ title: "Not deleted", description: e instanceof Error ? e.message : String(e), tone: "bad" }),
  });

  const users = q.data ?? [];
  const pending = users.filter((u) => u.status === "pending");
  const others = users.filter((u) => u.status !== "pending");
  const set = (u: AppUser, b: { role?: string; status?: string }, msg: string) => update.mutate({ u, b, msg });

  return (
    <>
      <PageHeader title="Users" description="Who can sign in to this SpecForge server. Everyone shares the same workspace; admins also manage users and the global settings."
        actions={!adding && <button className="primary" onClick={() => setAdding(true)}><UserPlus size={15} />Add user</button>} />
      {adding && <AddUser onDone={() => setAdding(false)} />}
      <Loading q={q} />

      {pending.length > 0 && (
        <section className="card flush">
          <div className="card-head" style={{ padding: "14px 18px 0" }}><h2 style={{ margin: 0 }}>Access requests</h2><span className="badge info">{pending.length} waiting</span></div>
          <div className="scroll"><table className="t">
            <thead><tr><th>User</th><th>Note</th><th>Requested</th><th style={{ textAlign: "right" }}>Decision</th></tr></thead>
            <tbody>{pending.map((u) => (
              <tr key={u.id}>
                <td>{who(u)}</td>
                <td className="small" style={{ maxWidth: 360, whiteSpace: "pre-wrap" }}>{u.note || <span className="muted">–</span>}</td>
                <td className="small muted">{when(u.created)}</td>
                <td><div className="row end" style={{ flexWrap: "nowrap" }}>
                  <button className="sm primary" disabled={update.isPending} onClick={() => set(u, { status: "active" }, `Approved ${u.username}`)}><Check size={13} />Approve</button>
                  <button className="sm danger" disabled={update.isPending} onClick={() => set(u, { status: "rejected" }, `Rejected ${u.username}`)}><X size={13} />Reject</button>
                </div></td>
              </tr>
            ))}</tbody>
          </table></div>
        </section>
      )}

      {q.data && (others.length === 0 ? <Empty icon={UsersIcon} title="No accounts yet">Add a user, or ask people to use <em>Request access</em> on the sign-in page.</Empty> : (
        <section className="card flush">
          <div className="scroll"><table className="t">
            <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Decided</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
            <tbody>{others.map((u) => {
              const self = u.id === me.id;
              return [
                <tr key={u.id}>
                  <td>{who(u)}{self && <span className="badge" style={{ marginTop: 4 }}>you</span>}</td>
                  <td>
                    <select value={u.role} disabled={self || update.isPending || u.status === "rejected"} aria-label={`Role of ${u.username}`} title={self ? "You cannot change your own role" : undefined}
                      onChange={(e) => set(u, { role: e.target.value }, `${u.username} is now ${e.target.value === "admin" ? "an admin" : "a user"}`)}>
                      <option value="user">User</option><option value="admin">Admin</option>
                    </select>
                  </td>
                  <td><span className={`badge ${STATUS[u.status]?.tone ?? ""}`}>{STATUS[u.status]?.label ?? u.status}</span></td>
                  <td className="small muted">{when(u.last_login)}</td>
                  <td className="small muted">{u.decided_by ? <>{when(u.decided_at)}<div>by {u.decided_by}</div></> : "–"}</td>
                  <td><div className="row end" style={{ flexWrap: "nowrap" }}>
                    {u.status === "active" && <button className="sm" disabled={self || update.isPending} title={self ? "You cannot disable your own account" : "Block sign-in and end their sessions"}
                      onClick={() => { if (confirm(`Disable ${u.username}? They are signed out at once and cannot sign in until re-enabled.`)) set(u, { status: "disabled" }, `Disabled ${u.username}`); }}>Disable</button>}
                    {(u.status === "disabled" || u.status === "rejected") && <button className="sm" disabled={update.isPending} onClick={() => set(u, { status: "active" }, `Enabled ${u.username}`)}>{u.status === "rejected" ? "Approve" : "Enable"}</button>}
                    <button className="sm" title="Set a new password" aria-label={`Reset password of ${u.username}`} onClick={() => setResetFor(resetFor === u.id ? null : u.id)}><KeyRound size={13} /></button>
                    <button className="sm danger" disabled={self || remove.isPending} title={self ? "You cannot delete your own account" : "Delete account"} aria-label={`Delete ${u.username}`}
                      onClick={() => { if (confirm(`Delete the account ${u.username}? The work they did stays in the shared workspace.`)) remove.mutate(u); }}><Trash2 size={13} /></button>
                  </div></td>
                </tr>,
                resetFor === u.id && <tr key={`${u.id}-pw`}><td colSpan={6}><ResetPassword user={u} onDone={() => setResetFor(null)} /></td></tr>,
              ];
            })}</tbody>
          </table></div>
        </section>
      ))}
      <p className="muted small">Locked out? On the server, <span className="mono">specforge user passwd &lt;name&gt;</span> sets a new password and <span className="mono">specforge user create &lt;name&gt; --admin</span> adds an admin.</p>
    </>
  );
}
