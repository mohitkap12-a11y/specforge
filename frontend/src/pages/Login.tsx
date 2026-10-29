import { useState, type FormEvent } from "react";
import { Layers } from "lucide-react";
import { api } from "../api";
import { ErrorNote, Field } from "../components/ui";

type Mode = "signin" | "request";

/** Shown instead of the app until the user signs in. New users request an account here; an admin approves it. */
export default function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [mode, setMode] = useState<Mode>("signin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [requested, setRequested] = useState(false);

  const switchTo = (m: Mode) => { setMode(m); setError(null); setPassword(""); setPassword2(""); setRequested(false); };
  const mismatch = mode === "request" && password2 !== "" && password !== password2;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      if (mode === "signin") {
        await api.login(username.trim(), password);
        onSignedIn();
      } else {
        await api.register({ username: username.trim(), password, display_name: name.trim() || undefined, note: note.trim() || undefined });
        setRequested(true); setPassword(""); setPassword2("");
      }
    } catch (err) { setError(err); } finally { setBusy(false); }
  };

  return (
    <div className="auth-wrap">
      <div className="auth-card card">
        <div className="auth-brand">
          <span className="brand-mark" aria-hidden="true"><Layers size={15} /></span>
          <div><strong>SpecForge</strong><div className="muted small">NVMe spec workbench</div></div>
        </div>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={mode === "signin"} className={mode === "signin" ? "active" : ""} onClick={() => switchTo("signin")}>Sign in</button>
          <button role="tab" aria-selected={mode === "request"} className={mode === "request" ? "active" : ""} onClick={() => switchTo("request")}>Request access</button>
        </div>
        {requested ? (
          <div className="stack">
            <div className="toast ok" role="status">Request sent for <strong>{username.trim()}</strong>. An admin has to approve it before you can sign in.</div>
            <button onClick={() => switchTo("signin")}>Back to sign in</button>
          </div>
        ) : (
          <form className="stack" onSubmit={submit}>
            <Field label="Username"><input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></Field>
            <Field label="Password" hint={mode === "request" ? "At least 8 characters" : undefined}>
              <input type="password" autoComplete={mode === "signin" ? "current-password" : "new-password"} value={password} onChange={(e) => setPassword(e.target.value)} required />
            </Field>
            {mode === "request" && <>
              <Field label="Repeat password" hint={mismatch ? "Passwords do not match" : undefined}>
                <input type="password" autoComplete="new-password" value={password2} onChange={(e) => setPassword2(e.target.value)} required aria-invalid={mismatch} />
              </Field>
              <Field label="Full name (optional)"><input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} /></Field>
              <Field label="Note for the admin (optional)" hint="Team, project or why you need access">
                <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
              </Field>
            </>}
            <ErrorNote error={error} />
            <button className="primary" type="submit" disabled={busy || !username.trim() || !password || (mode === "request" && (password.length < 8 || password !== password2))}>
              {busy && <span className="spinner" />}{mode === "signin" ? "Sign in" : "Send request"}
            </button>
            {mode === "signin" && <p className="muted small" style={{ margin: 0 }}>No account? Use <em>Request access</em>. Forgot your password? Ask an admin to reset it.</p>}
          </form>
        )}
      </div>
    </div>
  );
}
