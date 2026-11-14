import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../api";

const EMPTY_FORM = { username: "", email: "", role: "operator", password: "" };

export default function Users() {
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    try {
      setUsers(await apiRequest("/api/users"));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function updateField(event) {
    setForm((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  async function createUser(event) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await apiRequest("/api/users", { method: "POST", body: JSON.stringify(form) });
      setForm(EMPTY_FORM);
      setNotice("User created successfully.");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function deactivate(user) {
    if (!window.confirm(`Deactivate ${user.username}? Their active sessions will be revoked.`)) return;
    try {
      await apiRequest(`/api/users/${user.id}/deactivate`, { method: "POST" });
      setNotice(`${user.username} was deactivated.`);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="admin-page">
      <div className="page-heading">
        <div>
          <div className="kicker">Administration / access control</div>
          <h1>Users</h1>
          <p className="page-subtitle">Manage platform identities and role floors.</p>
        </div>
      </div>

      {error && <div className="notice notice-error">{error}</div>}
      {notice && <div className="notice notice-success">{notice}</div>}

      <div className="admin-grid">
        <article className="surface-panel">
          <div className="panel-heading">
            <div><div className="kicker">New identity</div><h2>Create user</h2></div>
          </div>
          <form className="stack-form" onSubmit={createUser}>
            <label>Username<input name="username" value={form.username} onChange={updateField} minLength="3" required /></label>
            <label>Email <span className="muted">optional</span><input name="email" type="email" value={form.email} onChange={updateField} /></label>
            <label>Role<select name="role" value={form.role} onChange={updateField}><option value="viewer">Viewer</option><option value="operator">Operator</option></select></label>
            <label>Temporary password<input name="password" type="password" value={form.password} onChange={updateField} minLength="12" required /></label>
            <p className="form-note">Passwords are hashed by the API and are never displayed again. Administrator accounts are provisioned out of band.</p>
            <button type="submit" disabled={saving}>{saving ? "Creating…" : "Create user"}</button>
          </form>
        </article>

        <article className="surface-panel panel-wide">
          <div className="panel-heading">
            <div><div className="kicker">Directory</div><h2>Platform users</h2></div>
            <span className="panel-meta">{users.length} identities</span>
          </div>
          {loading ? <div className="empty-state">Loading users…</div> : (
            <div className="table-wrap">
              <table className="grid admin-table"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Created</th><th /></tr></thead>
                <tbody>{users.map((user) => <tr key={user.id}>
                  <td><strong>{user.username}</strong><div className="muted small">{user.email || "No email"}</div></td>
                  <td><span className={`role role-${user.role}`}>{user.role}</span></td>
                  <td><span className={`status status-${user.is_active ? "online" : "offline"}`}>{user.is_active ? "active" : "inactive"}</span></td>
                  <td className="muted small">{new Date(user.created_at).toLocaleString()}</td>
                  <td>{user.is_active && <button className="button-quiet button-danger" onClick={() => deactivate(user)}>Deactivate</button>}</td>
                </tr>)}</tbody>
              </table>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
