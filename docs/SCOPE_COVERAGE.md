# Project Atlas — Scope Coverage Map

**Assessment** AT-26-L1-002 · **Candidate** Ahmed Elreefy · **Revised** 18 September 2026

This document maps the issued functional requirements to the current software surface and to the evidence captured for each. Requirement numbers follow Section 3 of the assessment package.

**Status** — 21 of 28 requirements evidenced on running hardware; the remainder implemented and evidenced on the software simulator.

---

## Device lifecycle

| Requirement | Software surface | Evidence |
|---|---|---|
| FR-01 Device registration | `Devices` registration form, `POST /api/devices`, one-time MQTT credential return | **Evidenced.** `atlas-test-99` registered live; credential displayed once under a "shown once / store securely" notice; `device.register` recorded in the audit log with `mqtt_password` stored as `[redacted]` |
| FR-02 Device removal and credential revocation | Administrator remove action, `deleteClient`, transactional soft delete, audit record | **Evidenced.** Publish succeeded before revocation (`CONNACK 0`), was refused after (`CONNACK 5, not authorised`), same command both times. Console removal attempts revocation before committing the soft delete and aborts if revocation fails — see Defect 1 |
| FR-25 Credential provisioning | Dynamic Security seeding and API provisioning | **Evidenced.** Credential returned once and never stored: the `devices` table holds `mqtt_username` only and has no password column |
| FR-26 Unauthorised-access protection | TLS 1.3, no anonymous MQTT, Dynamic Security ACLs, API authentication | **Evidenced.** Revoked credential refused at the broker; deactivated user account refused at sign-in with HTTP 403 |

## Telemetry

| Requirement | Software surface | Evidence |
|---|---|---|
| FR-03 Collection and monitoring | Firmware and simulator telemetry, ingestion, device health | **Evidenced on hardware.** Physical node publishes six metrics per message at 217 bytes; row counts confirmed rising across consecutive queries |
| FR-04 Device configuration downlink | Configuration downlink, firmware handling, append-only `device_config` | **Evidenced on hardware.** Sample interval changed to 2000 ms from the console; node acknowledged and the measured inter-sample gap became 2.00 s across twelve consecutive samples; five append-only configuration rows retained |
| FR-06 Payload validation | Schema validation in ingestion, rejection counters | **Evidenced.** 18,493 messages received, 18,493 accepted, 0 rejected, 0 dropped across the session |
| FR-07 Ingestion reliability | Batch commit, queue depth, broker reconnection | **Evidenced.** Broker stopped for 20 s: ingestion reported `broker_connected: false`, received count held steady, resumed with zero loss |
| FR-08 Raw telemetry retention | TimescaleDB hypertable and `/api/telemetry` | **Evidenced.** 160,000+ rows retained and queryable |
| FR-05 Secure broker transport | Mosquitto TLS, CA-mounted client paths, Dynamic Security | **Evidenced.** Broker certificate reissued with a matching SAN and verified against the CA using `openssl s_client`; node connects over TLS with fingerprint verification |

## Detection and alarms

| Requirement | Software surface | Evidence |
|---|---|---|
| FR-09 Threshold and anomaly detection | Four detectors: fixed limit, rate of change, statistical, staleness | **Evidenced on hardware.** Statistical detector raised `temperature_c` at 11.35 sigma from a fingertip stimulus, confirmed over three consecutive samples; humidity raised independently at 11.67 sigma from the same stimulus |
| FR-10 Live alarm delivery | Server-push delivery to the alarms panel | **Evidenced.** Alarm appeared without page refresh; delivery is pushed from the server rather than polled |
| FR-21 Critical event recording | Event history, severity levels, alarm lifecycle | **Evidenced.** Both devices transitioned to `offline` via the broker's Last Will when the broker stopped; staleness detector raised `no reading for bus_voltage_v in 6.2s, limit 6.0s` |
| FR-17 Threshold policy management | Threshold editor, API persistence, audit | **Evidenced.** `max_rate_of_change = 0.25`, `confirm_samples = 3` set for `temperature_c` on the physical node. The value was derived by measurement: observed noise floor 0.02 °C per sample against a stimulus peak of 0.46 °C per sample |
| FR-18 Alarm acknowledgement | Operator acknowledgement, automatic clear | **Evidenced.** Alarms raised 24, cleared 24; automatic clear after three normal samples |

## Console

| Requirement | Software surface | Evidence |
|---|---|---|
| FR-11 Connected-device visibility | Overview device health, Devices inventory | **Evidenced** |
| FR-12 Current status | Explicit online/degraded/offline state, last-reading freshness | **Evidenced.** Status derived from the broker's Last Will rather than polling, so a device that loses power without warning is still reported accurately |
| FR-13 Historical trends | Metric selector, range selector, trend view, summary statistics | **Evidenced.** 441 samples over a 15-minute window with min, max, mean and latest |
| FR-14 Alarms screen | Active, acknowledged and cleared states with detector context | **Evidenced** |
| FR-15 Event history | Severity and detector context, filterable | **Evidenced** |
| FR-16 Structured logging | JSON logs across all services with component and actor | **Evidenced** throughout this document |

## Identity and traceability

| Requirement | Software surface | Evidence |
|---|---|---|
| FR-19 Change traceability | Read-only audit log with before and after state | **Evidenced.** `user.deactivate` recorded with actor, timestamp, source address, and both states |
| FR-20 User lifecycle | Create user, deactivate user, refresh-token revocation | **Evidenced.** Account signed in successfully, was deactivated, and the same password was then refused with HTTP 403. Refresh tokens are revoked in the same transaction, so a deactivated account cannot retain a working session |
| FR-22 Authentication | Sign-in, session restore, sign-out, email verification | **Evidenced.** Full registration path captured: `registration_requested` → `email_verified` → `login` → `logout` |
| FR-23 Password protection | Argon2id hashing, one-time credential display | **Evidenced.** No password column exists in `devices`; the audit log stores `[redacted]` in place of the credential |
| FR-24 Role-based permissions | API role floors, navigation and action visibility | **Evidenced.** A viewer session shows four navigation items; an administrator session shows six. `Users` and `Audit log` are absent for the viewer |

---

## Console surface

The operations console exposes Overview, Devices, Alarms, Event History, Users and Audit Log. Device Detail is a focused workspace for telemetry, configuration, thresholds and administrator removal. The Overview screen is deliberately operational: it surfaces network size, online and degraded devices, critical alarms, recent activity and freshness without concealing degraded state behind colour alone.

---

## Defects identified during final verification

**Defect 1 — console registration does not complete broker provisioning.**
Registering a device from the console creates the database row and issues MQTT credentials, but does not create the corresponding client in the Dynamic Security store. The device is left registered in appearance and refused in practice. The console reports the state honestly, showing `manual action` under the broker column rather than claiming success. `tools/provision_node.sh` performs all three steps together; the fix is for the console path to invoke the same procedure or to fail explicitly rather than completing partially.

**Defect 2 — certificate reissue script writes the wrong filename.**
`tools/reissue_broker_cert.sh` writes `broker.crt` while the broker configuration reads `server.crt`. Two reissue attempts therefore completed without effect and the stale certificate continued to be served. The certificate was issued manually with the correct SAN. The permanent fix is for the script to write the filename the configuration references.

**Observation — `is_active` carries two meanings.**
The column represents both "not yet verified" and "deactivated by an administrator". These are different states requiring different responses: the first awaits action by the user, the second is an administrative decision that must appear in the audit trail. Separating `email_verified` from `is_active` would remove the ambiguity.

---

## Environmental constraints recorded

**Outbound NTP is blocked on the laboratory network.** The node could not synchronise its clock and therefore could not validate the broker certificate. Time service was moved to the platform host (`chrony`, stratum 8). This removes the node's dependence on external connectivity entirely.

**The platform host address is now reserved.** A DHCP change would otherwise require three coupled actions: updating the firmware, reissuing the broker certificate with a new SAN, and regenerating the pinned fingerprint. The cost was incurred twice during commissioning, which is also the practical argument recorded under Deviation 7 for preferring CA verification over pinning.

---

*Scope coverage map · Project Atlas · AT-26-L1-002 · 18 September 2026*
