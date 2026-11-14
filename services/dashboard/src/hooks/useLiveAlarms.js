/*
 * Project Atlas - live alarm channel
 * AT-26-L1-002 | completes FR-12
 *
 * Alarms arrive over a WebSocket rather than by polling. The path is:
 *
 *   anomaly engine writes an alarm
 *     -> PostgreSQL NOTIFY
 *       -> the API is LISTENing and forwards to every connected socket
 *         -> this hook
 *
 * No service calls another service directly, and the database cost is one
 * listening connection regardless of how many dashboards are open.
 *
 * Why this exists when the screens already poll: the alarm list refreshes
 * every five seconds, so in the worst case a critical alarm sits unseen for
 * five full seconds. The end-to-end latency budget in the engineering report
 * is 250 ms. Polling cannot meet that; pushing can.
 *
 * TOKEN IN THE QUERY STRING
 * The browser WebSocket API cannot set an Authorization header, so the access
 * token is passed as a query parameter - the API is written to expect it
 * there. This is worth being explicit about because query strings are
 * ordinarily the wrong place for a credential: they appear in server logs and
 * in browser history. Three things limit the exposure here. The token is the
 * short-lived access token, not the refresh token. The connection is to the
 * same origin over TLS in deployment, so it is not sent to a third party. And
 * the alternative - a subprotocol carrying the token - is not supported
 * uniformly enough to rely on. Recorded as a known trade-off rather than left
 * for a reviewer to find.
 */

import { useEffect, useRef, useState } from "react";
import { getAccessTokenForSocket } from "../api";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

/* Reconnect with exponential backoff. A tight retry loop against a broker or
 * API that is down turns one outage into two, and this dashboard is expected
 * to sit open for hours. */
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;

export function useLiveAlarms(onAlarm) {
  const [connected, setConnected] = useState(false);
  const socketRef = useRef(null);
  const backoffRef = useRef(BACKOFF_MIN_MS);
  const timerRef = useRef(null);
  const closedByUs = useRef(false);

  /* The callback is held in a ref so that a caller passing an inline function
   * does not tear down and rebuild the socket on every render. */
  const handlerRef = useRef(onAlarm);
  handlerRef.current = onAlarm;

  useEffect(() => {
    closedByUs.current = false;

    function connect() {
      const token = getAccessTokenForSocket();
      if (!token) {
        // No access token yet - the session restore has not finished. Try
        // again shortly rather than failing permanently.
        timerRef.current = setTimeout(connect, 1000);
        return;
      }

      const url =
        API_BASE.replace(/^http/, "ws") +
        `/ws/live?token=${encodeURIComponent(token)}`;

      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        setConnected(true);
        backoffRef.current = BACKOFF_MIN_MS;   // reset only on a real success
      };

      socket.onmessage = (event) => {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;                               // ignore anything unparseable
        }
        // The server sends periodic pings to keep intermediaries from
        // dropping an idle connection. They carry no payload and are not
        // alarms.
        if (msg.type === "alarm" && handlerRef.current) {
          handlerRef.current(msg.data);
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (closedByUs.current) return;
        timerRef.current = setTimeout(connect, backoffRef.current);
        backoffRef.current = Math.min(backoffRef.current * 2, BACKOFF_MAX_MS);
      };

      socket.onerror = () => {
        // onclose always follows onerror, so the reconnect is scheduled there
        // and not here - otherwise a failure would schedule two attempts.
        socket.close();
      };
    }

    connect();

    return () => {
      closedByUs.current = true;
      clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, []);

  return { connected };
}
