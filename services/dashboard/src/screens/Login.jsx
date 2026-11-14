import { useState } from "react";
import {
  login,
  registerAccount,
  requestPasswordReset,
  resetPassword,
  resendVerification,
  verifyEmail,
} from "../api";

const INITIAL = { username: "", email: "", password: "", code: "", newPassword: "" };

export default function Login({ onSignedIn }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState(INITIAL);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  function update(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
    setError(null);
    setNotice(null);
  }

  function switchMode(next) {
    setMode(next);
    setError(null);
    setNotice(null);
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "login") {
        const user = await login(form.username, form.password);
        onSignedIn(user);
      } else if (mode === "register") {
        await registerAccount(form.username, form.email, form.password);
        setNotice("If the account is eligible, a verification email has been sent.");
        setMode("verify");
      } else if (mode === "verify") {
        await verifyEmail(form.email, form.code);
        setNotice("Email verified. You can sign in now.");
        setMode("login");
      } else if (mode === "forgot") {
        await requestPasswordReset(form.email);
        setNotice("If the account is eligible, a reset code has been sent by email.");
        setMode("reset");
      } else if (mode === "reset") {
        await resetPassword(form.email, form.code, form.newPassword);
        setNotice("Password reset. Sign in with your new password.");
        setMode("login");
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const titles = {
    login: ["Welcome back", "Sign in to the operations console"],
    register: ["Create account", "New accounts start as Viewer until promoted"],
    verify: ["Verify email", "Enter the six-digit code sent to your email"],
    forgot: ["Recover access", "We will send a reset code if the account is eligible"],
    reset: ["Set new password", "Use the code from your reset email"],
  };

  return (
    <div className="login-page">
      <div className="login-card auth-card">
        <div className="kicker">Atlas identity gateway</div>
        <h1>Project Atlas</h1>
        <p className="subtitle">Cyber-Physical Intelligence Platform</p>
        <div className="auth-title"><strong>{titles[mode][0]}</strong><span>{titles[mode][1]}</span></div>

        <form onSubmit={submit}>
          {mode === "login" && <>
            <label htmlFor="username">Username</label>
            <input id="username" value={form.username} autoComplete="username" onChange={(e) => update("username", e.target.value)} required />
            <label htmlFor="password">Password</label>
            <input id="password" type="password" value={form.password} autoComplete="current-password" onChange={(e) => update("password", e.target.value)} required />
          </>}

          {mode === "register" && <>
            <label>Username<input value={form.username} autoComplete="username" onChange={(e) => update("username", e.target.value)} minLength="3" required /></label>
            <label>Email<input type="email" value={form.email} autoComplete="email" onChange={(e) => update("email", e.target.value)} required /></label>
            <label>Password<input type="password" value={form.password} autoComplete="new-password" onChange={(e) => update("password", e.target.value)} minLength="12" required /></label>
          </>}

          {(mode === "verify" || mode === "forgot" || mode === "reset") && <label>Email<input type="email" value={form.email} autoComplete="email" onChange={(e) => update("email", e.target.value)} required /></label>}
          {(mode === "verify" || mode === "reset") && <label>Verification code<input inputMode="numeric" pattern="[0-9]{6}" maxLength="6" value={form.code} onChange={(e) => update("code", e.target.value)} required /></label>}
          {mode === "reset" && <label>New password<input type="password" value={form.newPassword} autoComplete="new-password" onChange={(e) => update("newPassword", e.target.value)} minLength="12" required /></label>}

          {error && <div className="error">{error}</div>}
          {notice && <div className="notice notice-success">{notice}</div>}
          <button type="submit" disabled={busy}>{busy ? "Working…" : mode === "login" ? "Sign in" : mode === "register" ? "Create account" : mode === "verify" ? "Verify email" : mode === "forgot" ? "Send reset code" : "Reset password"}</button>
        </form>

        <div className="auth-links">
          {mode === "login" && <><button className="link-button" onClick={() => switchMode("register")}>Create an account</button><button className="link-button" onClick={() => switchMode("forgot")}>Forgot password?</button></>}
          {mode === "register" && <button className="link-button" onClick={() => switchMode("login")}>Already have an account? Sign in</button>}
          {mode === "verify" && <><button className="link-button" onClick={() => resendVerification(form.email)}>Resend verification email</button><button className="link-button" onClick={() => switchMode("login")}>Back to sign in</button></>}
          {mode === "forgot" && <button className="link-button" onClick={() => switchMode("login")}>Back to sign in</button>}
          {mode === "reset" && <button className="link-button" onClick={() => switchMode("login")}>Back to sign in</button>}
        </div>
      </div>
    </div>
  );
}
