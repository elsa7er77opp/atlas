import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../api";

function JsonValue({ value }) {
  if (!value) return <span className="muted">—</span>;
  return <pre className="json-cell">{JSON.stringify(value, null, 2)}</pre>;
}

export default function Audit() {
  const [rows, setRows] = useState([]);
  const [action, setAction] = useState("");
  const [entity, setEntity] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (action) params.set("action", action);
      if (entity) params.set("entity_type", entity);
      setRows(await apiRequest(`/api/audit?${params.toString()}`));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [action, entity]);

  useEffect(() => { load(); }, [load]);

  return (
    <section className="admin-page">
      <div className="page-heading">
        <div>
          <div className="kicker">Administration / traceability</div>
          <h1>Audit log</h1>
          <p className="page-subtitle">Read-only evidence of who changed what, and when.</p>
        </div>
        <span className="panel-meta">Last 7 days · {rows.length} records</span>
      </div>

      {error && <div className="notice notice-error">{error}</div>}
      <article className="surface-panel">
        <div className="filter-bar">
          <label>Action<input value={action} onChange={(event) => setAction(event.target.value)} placeholder="e.g. device.config.update" /></label>
          <label>Entity type<input value={entity} onChange={(event) => setEntity(event.target.value)} placeholder="device, user, threshold" /></label>
          <button className="button-quiet" onClick={load}>Refresh log</button>
        </div>
      </article>

      <article className="surface-panel audit-panel">
        {loading ? <div className="empty-state">Loading audit records…</div> : rows.length === 0 ? <div className="empty-state">No audit records match these filters.</div> : (
          <div className="table-wrap"><table className="grid audit-table"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Before</th><th>After</th><th>Source</th></tr></thead>
            <tbody>{rows.map((row) => <tr key={row.id}>
              <td className="muted small">{new Date(row.occurred_at).toLocaleString()}</td>
              <td><strong>{row.username || "system"}</strong></td>
              <td><span className="audit-action">{row.action}</span></td>
              <td className="mono">{row.entity_type || "—"} {row.entity_id ?? ""}</td>
              <td><JsonValue value={row.before} /></td>
              <td><JsonValue value={row.after} /></td>
              <td className="muted small">{row.ip_address || "—"}</td>
            </tr>)}</tbody>
          </table></div>
        )}
      </article>
    </section>
  );
}
