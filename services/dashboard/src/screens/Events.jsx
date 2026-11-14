/*
 * Project Atlas - event history
 * AT-26-L1-002 | serves FR-15 (event history)
 */

import { useEffect, useState, useCallback } from "react";
import { apiRequest } from "../api";

const PAGE = 50;

const SEVERITIES = [
  { key: "",         label: "All" },
  { key: "info",     label: "Info" },
  { key: "warning",  label: "Warning" },
  { key: "critical", label: "Critical" },
];

export default function Events({ devices }) {
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useState(0);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
      if (severity) q.set("severity", severity);
      setEvents(await apiRequest(`/api/events?${q}`));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [severity, offset]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  /* Only the first page auto-refreshes. Polling while the user is reading page
   * four would shift rows under them as new events arrive, which makes the
   * history unreadable exactly when they are trying to follow a sequence. */
  useEffect(() => {
    if (offset !== 0) return;
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [offset, load]);

  function changeSeverity(key) {
    setSeverity(key);
    setOffset(0);        // a filtered page 4 of an unfiltered list is meaningless
  }

  const deviceName = (id) =>
    devices.find((d) => d.id === id)?.device_uid ?? `device ${id}`;

  return (
    <section>
      <div className="section-head">
        <h2>Event history</h2>
        <div className="range-buttons">
          {SEVERITIES.map((s) => (
            <button
              key={s.key}
              className={`chip ${s.key === severity ? "chip-on" : ""}`}
              onClick={() => changeSeverity(s.key)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {loading && events.length === 0 ? (
        <div className="muted">Loading…</div>
      ) : events.length === 0 ? (
        <div className="empty">No events recorded.</div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Time</th>
              <th>Severity</th>
              <th>Device</th>
              <th>Detector</th>
              <th>Metric</th>
              <th>Observed</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td className="muted small nowrap">
                  {new Date(e.occurred_at).toLocaleString()}
                </td>
                <td>
                  <span className={`sev sev-${e.severity}`}>{e.severity}</span>
                </td>
                <td className="mono small">{deviceName(e.device_id)}</td>
                <td className="mono small">{e.detector}</td>
                <td className="mono small">{e.metric || "—"}</td>
                <td className="mono small">
                  {e.observed_value === null || e.observed_value === undefined
                    ? "—"
                    : e.observed_value}
                </td>
                {/* The message states the numeric condition that fired. Every
                    alarm traces to a named detector, a metric and a number -
                    which is what makes the detection explainable rather than
                    something the operator has to take on trust. */}
                <td className="small">{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="pager">
        <button
          className="chip"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          ← Newer
        </button>
        <span className="muted small">
          {offset + 1}–{offset + events.length}
        </span>
        <button
          className="chip"
          /* The API returns a page, not a total, so "is there more" is
             inferred from a full page rather than asserted. */
          disabled={events.length < PAGE}
          onClick={() => setOffset(offset + PAGE)}
        >
          Older →
        </button>
      </div>
    </section>
  );
}
