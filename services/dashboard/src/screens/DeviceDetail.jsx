/*
 * Project Atlas - device detail and historical trend
 * AT-26-L1-002 | serves FR-13 (historical trends)
 */

import { useEffect, useState, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { apiRequest, getCurrentUser } from "../api";

/* The nine metrics of the payload contract, with the units they are published
 * in. Units are not decoration: a current chart labelled only "current" is
 * ambiguous between amps and milliamps, and the detector thresholds are
 * expressed in milliamps. */
const METRICS = [
  { key: "current_ma",    label: "Current",     unit: "mA" },
  { key: "bus_voltage_v", label: "Bus voltage", unit: "V" },
  { key: "power_mw",      label: "Power",       unit: "mW" },
  { key: "temperature_c", label: "Temperature", unit: "°C" },
  { key: "humidity_pct",  label: "Humidity",    unit: "%" },
  { key: "pressure_hpa",  label: "Pressure",    unit: "hPa" },
  { key: "accel_x_g",     label: "Accel X",     unit: "g" },
  { key: "accel_y_g",     label: "Accel Y",     unit: "g" },
  { key: "accel_z_g",     label: "Accel Z",     unit: "g" },
];

const RANGES = [
  { key: "5m",  label: "5 min",  minutes: 5 },
  { key: "15m", label: "15 min", minutes: 15 },
  { key: "1h",  label: "1 hour", minutes: 60 },
  { key: "6h",  label: "6 hours", minutes: 360 },
];

const THRESHOLD_FIELDS = [
  ["min_value", "Minimum"],
  ["max_value", "Maximum"],
  ["max_rate_of_change", "Max rate of change"],
  ["z_window", "Statistical window"],
  ["z_sigma", "Z-score sigma"],
  ["confirm_samples", "Confirm samples"],
];

export default function DeviceDetail({ device, onBack }) {
  const [metric, setMetric] = useState("current_ma");
  const [range, setRange] = useState(RANGES[1]);
  const [points, setPoints] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [configDraft, setConfigDraft] = useState({ sample_interval_ms: 1000 });
  const [configSaving, setConfigSaving] = useState(false);
  const [configNotice, setConfigNotice] = useState(null);
  const [thresholds, setThresholds] = useState([]);
  const [thresholdMetric, setThresholdMetric] = useState("current_ma");
  const [thresholdDraft, setThresholdDraft] = useState({});
  const [thresholdSaving, setThresholdSaving] = useState(false);
  const [thresholdNotice, setThresholdNotice] = useState(null);
  const [removeNotice, setRemoveNotice] = useState(null);
  const canConfigure = ["operator", "administrator"].includes(getCurrentUser()?.role);
  const canRemove = getCurrentUser()?.role === "administrator";

  const load = useCallback(async () => {
    try {
      const start = new Date(Date.now() - range.minutes * 60_000).toISOString();

      /* Raw readings rather than /api/telemetry/aggregate. At 1 Hz a six-hour
       * window is 21,600 points, which is more than a chart can usefully draw
       * and more than a browser should hold. The limit below caps it; the
       * aggregate endpoint is the correct answer for longer windows and is
       * left for when the retention period makes those windows meaningful. */
      const rows = await apiRequest(
        `/api/telemetry?device_id=${device.id}` +
        `&metric=${encodeURIComponent(metric)}` +
        `&start=${encodeURIComponent(start)}&limit=2000`
      );

      /* The API returns newest first, which is right for a table and wrong for
       * a chart: plotted in that order the time axis runs backwards. */
      const ordered = [...rows].reverse().map((r) => ({
        t: new Date(r.time).getTime(),
        value: r.value,
        quality: r.quality,
      }));

      setPoints(ordered);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [device.id, metric, range]);

  useEffect(() => {
    setLoading(true);
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    apiRequest(`/api/devices/${device.id}`)
      .then((detail) => setConfigDraft({
        sample_interval_ms: detail.sample_interval_ms ?? 1000,
      }))
      .catch((e) => setError(e.message));
  }, [device.id]);

  useEffect(() => {
    apiRequest(`/api/devices/${device.id}/thresholds`)
      .then(setThresholds)
      .catch((e) => setError(e.message));
  }, [device.id]);

  useEffect(() => {
    const existing = thresholds.find((threshold) => threshold.metric === thresholdMetric) || {};
    setThresholdDraft(Object.fromEntries(THRESHOLD_FIELDS.map(([key]) => [key, existing[key] ?? ""])));
  }, [thresholds, thresholdMetric]);

  async function saveThresholds(event) {
    event.preventDefault();
    setThresholdSaving(true);
    setThresholdNotice(null);
    try {
      const body = Object.fromEntries(Object.entries(thresholdDraft)
        .filter(([, value]) => value !== "")
        .map(([key, value]) => [key, Number(value)]));
      const updated = await apiRequest(`/api/devices/${device.id}/thresholds/${thresholdMetric}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      setThresholds((current) => [...current.filter((threshold) => threshold.metric !== thresholdMetric), updated]);
      setThresholdNotice(`Thresholds saved for ${thresholdMetric}.`);
    } catch (e) {
      setThresholdNotice(e.message);
    } finally {
      setThresholdSaving(false);
    }
  }

  async function removeDevice() {
    if (!window.confirm(`Remove ${device.device_uid}? Broker access will be revoked before the device is soft-deleted.`)) return;
    setRemoveNotice(null);
    try {
      await apiRequest(`/api/devices/${device.id}`, { method: "DELETE" });
      onBack();
    } catch (e) {
      setRemoveNotice(e.message);
    }
  }

  async function saveConfig(event) {
    event.preventDefault();
    setConfigSaving(true);
    setConfigNotice(null);
    try {
      await apiRequest(`/api/devices/${device.id}/config`, {
        method: "PATCH",
        body: JSON.stringify({
          sample_interval_ms: Number(configDraft.sample_interval_ms),
        }),
      });
      setConfigNotice("Configuration saved and queued for the device.");
    } catch (e) {
      setConfigNotice(e.message);
    } finally {
      setConfigSaving(false);
    }
  }

  const unit = METRICS.find((m) => m.key === metric)?.unit ?? "";

  /* Computed from what is actually plotted, so the summary and the chart can
   * never disagree. */
  const values = points.map((p) => p.value);
  const stats = values.length
    ? {
        latest: values[values.length - 1],
        min: Math.min(...values),
        max: Math.max(...values),
        mean: values.reduce((a, b) => a + b, 0) / values.length,
      }
    : null;

  const degraded = points.filter((p) => p.quality !== "ok").length;

  return (
    <section>
      <div className="section-head">
        <div>
          <button className="link" onClick={onBack}>← Devices</button>
          <h2 style={{ marginTop: "0.4rem" }}>{device.name}</h2>
          <div className="muted small mono">{device.device_uid}</div>
        </div>
        <span className={`status status-${device.status}`}>{device.status}</span>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="controls">
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>
          {METRICS.map((m) => (
            <option key={m.key} value={m.key}>
              {m.label} ({m.unit})
            </option>
          ))}
        </select>

        <div className="range-buttons">
          {RANGES.map((r) => (
            <button
              key={r.key}
              className={`chip ${r.key === range.key ? "chip-on" : ""}`}
              onClick={() => setRange(r)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {canConfigure && (
        <form className="config-panel" onSubmit={saveConfig}>
          <div>
            <div className="stat-label">Device configuration</div>
            <label className="config-label">
              Sample interval (ms)
              <input
                type="number"
                min="100"
                max="3600000"
                step="100"
                value={configDraft.sample_interval_ms}
                onChange={(e) => setConfigDraft({ sample_interval_ms: e.target.value })}
              />
            </label>
          </div>
          <button type="submit" disabled={configSaving}>
            {configSaving ? "Saving…" : "Apply to device"}
          </button>
          {configNotice && <div className="small muted">{configNotice}</div>}
        </form>
      )}

      {canConfigure && (
        <form className="surface-panel threshold-panel" onSubmit={saveThresholds}>
          <div className="panel-heading"><div><div className="kicker">Detection policy</div><h2>Thresholds</h2></div><span className="panel-meta">FR-04 / audit recorded</span></div>
          <div className="threshold-toolbar">
            <label>Metric<select value={thresholdMetric} onChange={(event) => setThresholdMetric(event.target.value)}>{METRICS.map((item) => <option key={item.key} value={item.key}>{item.label} ({item.unit})</option>)}</select></label>
            <span className="muted small">Leave a field blank to keep it unset.</span>
          </div>
          <div className="threshold-grid">{THRESHOLD_FIELDS.map(([key, label]) => <label key={key}>{label}<input type="number" step="any" value={thresholdDraft[key] ?? ""} onChange={(event) => setThresholdDraft((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
          <div className="threshold-actions"><button className="button-primary" type="submit" disabled={thresholdSaving}>{thresholdSaving ? "Saving…" : "Save threshold policy"}</button>{thresholdNotice && <span className="small muted">{thresholdNotice}</span>}</div>
        </form>
      )}

      {canRemove && <div className="danger-zone"><div><div className="kicker">Administrator action</div><strong>Remove device</strong><p className="muted small">Broker credential revocation is attempted before the soft delete is committed.</p></div><button className="button-quiet button-danger" onClick={removeDevice}>Remove {device.device_uid}</button>{removeNotice && <div className="small remove-error">{removeNotice}</div>}</div>}

      {stats && (
        <div className="stats">
          <Stat label="Latest" value={stats.latest} unit={unit} />
          <Stat label="Minimum" value={stats.min} unit={unit} />
          <Stat label="Maximum" value={stats.max} unit={unit} />
          <Stat label="Mean" value={stats.mean} unit={unit} />
          <Stat label="Samples" value={points.length} unit="" raw />
        </div>
      )}

      {degraded > 0 && (
        /* A degraded reading means the firmware published a value it does not
         * fully trust - typically a sensor that failed to respond. Plotting it
         * silently alongside good data would misrepresent the series. */
        <div className="notice">
          {degraded} of {points.length} readings are marked degraded.
        </div>
      )}

      <div className="chart-panel">
        {loading && points.length === 0 ? (
          <div className="muted">Loading…</div>
        ) : points.length === 0 ? (
          <div className="muted">
            No readings for {metric} in the last {range.label}.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={320}>
            <LineChart data={points} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
              <CartesianGrid stroke="#2a3441" strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t) =>
                  new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                }
                stroke="#8b98a5"
                fontSize={12}
              />
              <YAxis
                stroke="#8b98a5"
                fontSize={12}
                width={64}
                /* Not forced to include zero. A bus voltage that sits at 5.00 V
                 * with millivolt noise becomes a flat line against a 0-5 axis,
                 * hiding exactly the variation worth seeing. */
                domain={["auto", "auto"]}
                tickFormatter={(v) => v.toFixed(2)}
              />
              <Tooltip
                contentStyle={{
                  background: "#161b22",
                  border: "1px solid #2a3441",
                  borderRadius: 6,
                  fontSize: 13,
                }}
                labelFormatter={(t) => new Date(t).toLocaleString()}
                formatter={(v) => [`${v} ${unit}`, metric]}
              />
              <Line
                type="monotone"
                dataKey="value"
                stroke="#2f81f7"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}

function Stat({ label, value, unit, raw }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {raw ? value : Number(value).toFixed(2)}
        {unit && <span className="stat-unit"> {unit}</span>}
      </div>
    </div>
  );
}
