import { useCallback, useEffect, useMemo, useState } from "react";
import { apiRequest } from "../api";

function ageLabel(iso) {
  if (!iso) return "No reading yet";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 10) return "Just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function MetricCard({ eyebrow, value, label, tone = "neutral" }) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <div className="metric-eyebrow">{eyebrow}</div>
      <div className="metric-value">{value}</div>
      <div className="metric-label">{label}</div>
    </article>
  );
}

export default function Overview({ onSelectDevice }) {
  const [devices, setDevices] = useState([]);
  const [alarms, setAlarms] = useState([]);
  const [events, setEvents] = useState([]);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    const results = await Promise.allSettled([
      apiRequest("/api/devices"),
      apiRequest("/api/alarms?state=active&limit=8"),
      apiRequest("/api/events?limit=8"),
      apiRequest("/health"),
    ]);
    const [deviceResult, alarmResult, eventResult, healthResult] = results;
    if (deviceResult.status === "fulfilled") setDevices(deviceResult.value);
    if (alarmResult.status === "fulfilled") setAlarms(alarmResult.value);
    if (eventResult.status === "fulfilled") setEvents(eventResult.value);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    if (results.some((result) => result.status === "rejected")) {
      setError("One or more telemetry sources are temporarily unavailable.");
    } else {
      setError(null);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const counts = useMemo(() => ({
    online: devices.filter((device) => device.status === "online").length,
    degraded: devices.filter((device) => device.status === "degraded").length,
    critical: alarms.filter((alarm) => alarm.severity === "critical").length,
  }), [devices, alarms]);

  return (
    <section className="overview-page">
      <div className="page-heading">
        <div>
          <div className="kicker">Operations / live system</div>
          <h1>Control room</h1>
          <p className="page-subtitle">A single view of the Atlas sensing network.</p>
        </div>
        <div className={`system-pulse ${health?.status === "ok" ? "pulse-ok" : "pulse-warn"}`}>
          <span className="pulse-dot" />
          {health?.status === "ok" ? "Platform nominal" : "Platform degraded"}
        </div>
      </div>

      {error && <div className="notice notice-warning">{error}</div>}

      <div className="metric-grid">
        <MetricCard eyebrow="Network" value={devices.length} label="Registered devices" />
        <MetricCard eyebrow="Live now" value={counts.online} label="Online devices" tone="ok" />
        <MetricCard eyebrow="Attention" value={counts.degraded} label="Degraded devices" tone={counts.degraded ? "warn" : "neutral"} />
        <MetricCard eyebrow="Response" value={counts.critical} label="Critical alarms" tone={counts.critical ? "critical" : "ok"} />
      </div>

      <div className="overview-grid">
        <article className="surface-panel panel-wide">
          <div className="panel-heading">
            <div>
              <div className="kicker">Asset health</div>
              <h2>Device network</h2>
            </div>
            <span className="panel-meta">Auto-refresh 10s</span>
          </div>
          <div className="device-health-list">
            {devices.length === 0 ? (
              <div className="empty-state">No devices registered yet.</div>
            ) : devices.map((device) => (
              <button key={device.id} className="device-health-row" onClick={() => onSelectDevice(device)}>
                <span className={`status status-${device.status}`} />
                <span className="device-health-main">
                  <strong>{device.name}</strong>
                  <span className="muted mono">{device.device_uid}</span>
                </span>
                <span className="device-health-site">{device.site}</span>
                <span className="device-health-freshness">{ageLabel(device.last_seen_at)}</span>
                <span className="row-chevron">→</span>
              </button>
            ))}
          </div>
        </article>

        <article className="surface-panel activity-panel">
          <div className="panel-heading">
            <div>
              <div className="kicker">Attention queue</div>
              <h2>Active alarms</h2>
            </div>
            <span className="count-badge">{alarms.length}</span>
          </div>
          {alarms.length === 0 ? (
            <div className="empty-state empty-state-quiet"><span className="check-mark">✓</span> No active alarms</div>
          ) : (
            <div className="activity-list">
              {alarms.slice(0, 5).map((alarm) => (
                <div className="activity-item" key={alarm.id}>
                  <span className={`severity-bar severity-${alarm.severity}`} />
                  <div>
                    <strong>{alarm.severity} alarm</strong>
                    <span className="muted small">Device {alarm.device_id} · {new Date(alarm.raised_at).toLocaleString()}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </article>
      </div>

      <article className="surface-panel">
        <div className="panel-heading">
          <div>
            <div className="kicker">Audit stream</div>
            <h2>Recent events</h2>
          </div>
          <span className="panel-meta">Last 8 events</span>
        </div>
        {events.length === 0 ? (
          <div className="empty-state">No recent events.</div>
        ) : (
          <div className="event-strip">
            {events.slice(0, 6).map((event) => (
              <div className="event-card" key={event.id}>
                <span className={`event-severity event-${event.severity}`}>{event.severity}</span>
                <strong>{event.detector}</strong>
                <p>{event.message}</p>
                <span className="muted small">{new Date(event.occurred_at).toLocaleString()}</span>
              </div>
            ))}
          </div>
        )}
      </article>
    </section>
  );
}
