import { useCallback, useEffect, useState } from "react";
import { apiRequest, getCurrentUser } from "../api";

const EMPTY_FORM = { device_uid: "", name: "", site: "lab-a", location: "", sample_interval_ms: 1000 };

function relativeTime(iso) {
  if (!iso) return "never";
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function Devices({ onSelect }) {
  const [devices, setDevices] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [credential, setCredential] = useState(null);
  const isAdmin = getCurrentUser()?.role === "administrator";

  const load = useCallback(async () => {
    try {
      setDevices(await apiRequest("/api/devices"));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  function updateField(event) {
    setForm((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  async function registerDevice(event) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setCredential(null);
    try {
      const result = await apiRequest("/api/devices", {
        method: "POST",
        body: JSON.stringify({ ...form, sample_interval_ms: Number(form.sample_interval_ms) }),
      });
      setCredential({ username: result.mqtt_username, password: result.mqtt_password, uid: result.device.device_uid });
      setForm(EMPTY_FORM);
      setShowForm(false);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function copyCredential(value) {
    await navigator.clipboard?.writeText(value);
  }

  if (loading) return <div className="empty-state">Loading device network…</div>;

  return (
    <section className="devices-page">
      <div className="page-heading">
        <div>
          <div className="kicker">Workspace / asset network</div>
          <h1>Devices</h1>
          <p className="page-subtitle">Monitor connectivity, freshness, and broker provisioning.</p>
        </div>
        <div className="heading-actions">
          <span className="network-summary"><strong>{devices.filter((d) => d.status === "online").length}</strong> / {devices.length} online</span>
          {isAdmin && <button className="button-primary" onClick={() => setShowForm((value) => !value)}>{showForm ? "Close form" : "+ Register device"}</button>}
        </div>
      </div>

      {error && <div className="notice notice-error">{error}</div>}
      {credential && <div className="credential-panel">
        <div><div className="kicker">Shown once / store securely</div><strong>MQTT credential created for {credential.uid}</strong><p className="muted small">This password is not recoverable from Atlas after leaving this screen.</p></div>
        <div className="credential-values"><code>{credential.username}</code><button className="button-quiet" onClick={() => copyCredential(credential.username)}>Copy user</button><code>{credential.password}</code><button className="button-quiet" onClick={() => copyCredential(credential.password)}>Copy password</button></div>
      </div>}

      {showForm && isAdmin && <article className="surface-panel register-panel">
        <div className="panel-heading"><div><div className="kicker">Provisioning</div><h2>Register a device</h2></div><span className="panel-meta">Creates MQTT credentials</span></div>
        <form className="register-grid" onSubmit={registerDevice}>
          <label>Device UID<input name="device_uid" value={form.device_uid} onChange={updateField} placeholder="atlas-node-02" pattern="[a-z0-9][a-z0-9-]{2,63}" required /></label>
          <label>Display name<input name="name" value={form.name} onChange={updateField} placeholder="North sensor" required /></label>
          <label>Site<input name="site" value={form.site} onChange={updateField} placeholder="lab-a" required /></label>
          <label>Location<input name="location" value={form.location} onChange={updateField} placeholder="Rack 2 / bay 4" /></label>
          <label>Sample interval (ms)<input type="number" name="sample_interval_ms" min="100" max="3600000" step="100" value={form.sample_interval_ms} onChange={updateField} required /></label>
          <div className="register-submit"><button className="button-primary" disabled={saving}>{saving ? "Provisioning…" : "Register device"}</button></div>
        </form>
      </article>}

      <article className="surface-panel">
        <div className="panel-heading"><div><div className="kicker">Live inventory</div><h2>Network devices</h2></div><span className="panel-meta">Refreshes every 5s</span></div>
        <div className="table-wrap"><table className="grid"><thead><tr><th>Device</th><th>Identifier</th><th>Site</th><th>Status</th><th>Last reading</th><th>Broker</th></tr></thead>
          <tbody>{devices.map((device) => <tr key={device.id} onClick={() => onSelect && onSelect(device)} className="clickable">
            <td><strong>{device.name}</strong><div className="muted small">{device.location || "Location not set"}</div></td>
            <td className="mono">{device.device_uid}</td>
            <td>{device.site}</td>
            <td><span className={`status status-${device.status}`}>{device.status}</span></td>
            <td className="muted">{relativeTime(device.last_seen_at)}</td>
            <td>{device.broker_provisioned ? <span className="status status-online">provisioned</span> : <span className="warn small">manual action</span>}</td>
          </tr>)}</tbody>
        </table></div>
        {devices.length === 0 && <div className="empty-state">No devices registered yet.</div>}
      </article>
    </section>
  );
}
