/*
 * Project Atlas - alarms
 * AT-26-L1-002 | serves FR-14 (active alarms)
 */

import { useEffect, useState, useCallback } from "react";
import { apiRequest, getCurrentUser } from "../api";
import { useLiveAlarms } from "../hooks/useLiveAlarms";

const STATES = [
  { key: "active",       label: "Active" },
  { key: "acknowledged", label: "Acknowledged" },
  { key: "cleared",      label: "Cleared" },
];

export default function Alarms({ devices }) {
  const [state, setState] = useState("active");
  const [alarms, setAlarms] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  const user = getCurrentUser();


  /* Acknowledgement is an operator action. Hiding the button from a viewer is
   * a courtesy, not a control: the API refuses the call regardless of what
   * the interface shows, and that refusal is what verify_api.sh tests. */
  const canAcknowledge = user?.role === "operator" || user?.role === "administrator";

  const load = useCallback(async () => {
    try {
      setAlarms(await apiRequest(`/api/alarms?state=${state}&limit=100`));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [state]);

  /* A pushed alarm triggers a reload rather than being inserted into the list
   * directly. The notification carries the alarm, but this list is filtered by
   * state and the server is the authority on what belongs in it - inserting
   * locally would drift from the server's view the moment an alarm is
   * acknowledged in another tab. A reload costs one query and cannot disagree
   * with the database. */
  const { connected } = useLiveAlarms(load);

  useEffect(() => {
    setLoading(true);
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  /* The poll stays. It is now a fallback for a dropped socket rather than the
   * primary path, which is why the interval is unchanged: if the WebSocket is
   * healthy the poll finds nothing new, and if it is not, the screen still
   * works. Removing it would make a socket failure silent. */

  async function acknowledge(id) {
    setBusyId(id);
    try {
      await apiRequest(`/api/alarms/${id}/ack`, { method: "POST" });
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  const deviceName = (id) =>
    devices.find((d) => d.id === id)?.device_uid ?? `device ${id}`;

  return (
    <section>
      <div className="section-head">
        <div>
          <h2>Alarms</h2>
          <span className={`live ${connected ? "live-on" : "live-off"}`}>
            {connected ? "live" : "reconnecting"}
          </span>
        </div>
        <div className="range-buttons">
          {STATES.map((s) => (
            <button
              key={s.key}
              className={`chip ${s.key === state ? "chip-on" : ""}`}
              onClick={() => setState(s.key)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {loading && alarms.length === 0 ? (
        <div className="muted">Loading…</div>
      ) : alarms.length === 0 ? (
        /* An empty active list is the normal, healthy state of a monitoring
         * system. It should read as "nothing wrong", not as a broken screen. */
        <div className="empty">
          {state === "active"
            ? "No active alarms."
            : `No ${state} alarms.`}
        </div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Severity</th>
              <th>Device</th>
              <th>Raised</th>
              <th>State</th>
              <th>Acknowledged by</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {alarms.map((a) => (
              <tr key={a.id}>
                <td>
                  {/* Written out, never colour alone. */}
                  <span className={`sev sev-${a.severity}`}>{a.severity}</span>
                </td>
                <td className="mono">{deviceName(a.device_id)}</td>
                <td className="muted small">
                  {new Date(a.raised_at).toLocaleString()}
                </td>
                <td>{a.state}</td>
                <td className="muted small">{a.acknowledged_by || "—"}</td>
                <td>
                  {a.state === "active" && canAcknowledge && (
                    <button
                      className="chip"
                      disabled={busyId === a.id}
                      onClick={() => acknowledge(a.id)}
                    >
                      {busyId === a.id ? "…" : "Acknowledge"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="muted small" style={{ marginTop: "1rem" }}>
        An event records that a condition occurred. An alarm is a state that
        persists until it clears — subsequent qualifying events update the
        existing alarm rather than raising a duplicate.
      </p>
    </section>
  );
}
