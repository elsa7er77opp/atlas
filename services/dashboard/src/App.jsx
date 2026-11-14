import { useEffect, useState } from "react";
import { restoreSession, logout, getCurrentUser, apiRequest } from "./api";
import Login from "./screens/Login";
import Overview from "./screens/Overview";
import Devices from "./screens/Devices";
import DeviceDetail from "./screens/DeviceDetail";
import Alarms from "./screens/Alarms";
import Events from "./screens/Events";
import Users from "./screens/Users";
import Audit from "./screens/Audit";
import "./App.css";

const PRIMARY_NAV = [
  { key: "overview", label: "Overview", icon: "⌂" },
  { key: "devices", label: "Devices", icon: "▦" },
  { key: "alarms", label: "Alarms", icon: "△" },
  { key: "events", label: "Event history", icon: "≡" },
];

const ADMIN_NAV = [
  { key: "users", label: "Users", icon: "◎" },
  { key: "audit", label: "Audit log", icon: "◌" },
];

export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  const [tab, setTab] = useState("overview");
  const [selected, setSelected] = useState(null);
  const [devices, setDevices] = useState([]);
  const [apiHealth, setApiHealth] = useState("checking");

  useEffect(() => {
    restoreSession()
      .then(setUser)
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!user) return;
    const load = () => apiRequest("/api/devices").then(setDevices).catch(() => {});
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [user]);

  useEffect(() => {
    if (!user) return;
    const check = () => apiRequest("/health").then(() => setApiHealth("ok")).catch(() => setApiHealth("degraded"));
    check();
    const timer = setInterval(check, 15000);
    return () => clearInterval(timer);
  }, [user]);

  async function signOut() {
    await logout();
    setUser(null);
    setSelected(null);
  }

  function navigate(nextTab) {
    setTab(nextTab);
    setSelected(null);
  }

  if (checking) return <div className="loading"><div className="loading-mark">AT</div><span>Restoring secure session…</span></div>;
  if (!user) return <Login onSignedIn={() => setUser(getCurrentUser())} />;

  const isAdmin = user.role === "administrator";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">AT</div>
          <div><strong>ATLAS</strong><span>Operations console</span></div>
        </div>
        <div className="sidebar-section-label">Workspace</div>
        <nav className="sidebar-nav">
          {PRIMARY_NAV.map((item) => (
            <button key={item.key} className={`side-nav-item ${tab === item.key && !selected ? "side-nav-active" : ""}`} onClick={() => navigate(item.key)}>
              <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
              {item.key === "alarms" && <span className="nav-live-dot" />}
            </button>
          ))}
        </nav>
        {isAdmin && <>
          <div className="sidebar-section-label sidebar-admin-label">Administration</div>
          <nav className="sidebar-nav">
            {ADMIN_NAV.map((item) => (
              <button key={item.key} className={`side-nav-item ${tab === item.key && !selected ? "side-nav-active" : ""}`} onClick={() => navigate(item.key)}>
                <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
              </button>
            ))}
          </nav>
        </>}
        <div className="sidebar-spacer" />
        <div className={`system-status ${apiHealth === "ok" ? "system-ok" : "system-warning"}`}>
          <span className="pulse-dot" />
          <div><strong>{apiHealth === "ok" ? "API connected" : "Checking API"}</strong><span>Secure local bridge</span></div>
        </div>
        <div className="sidebar-user">
          <div className="avatar">{user.username.slice(0, 1).toUpperCase()}</div>
          <div className="sidebar-user-copy"><strong>{user.username}</strong><span>{user.role}</span></div>
          <button className="icon-button" title="Sign out" onClick={signOut}>↗</button>
        </div>
      </aside>

      <main className="main-canvas">
        <header className="mobile-topbar">
          <div className="brand-block"><div className="brand-mark">AT</div><strong>ATLAS</strong></div>
          <button className="icon-button" onClick={signOut}>↗</button>
        </header>
        {selected ? (
          <DeviceDetail device={selected} onBack={() => setSelected(null)} />
        ) : tab === "overview" ? (
          <Overview onSelectDevice={setSelected} />
        ) : tab === "devices" ? (
          <Devices onSelect={setSelected} />
        ) : tab === "alarms" ? (
          <Alarms devices={devices} />
        ) : tab === "events" ? (
          <Events devices={devices} />
        ) : tab === "users" && isAdmin ? (
          <Users />
        ) : tab === "audit" && isAdmin ? (
          <Audit />
        ) : (
          <Overview onSelectDevice={setSelected} />
        )}
      </main>
    </div>
  );
}
