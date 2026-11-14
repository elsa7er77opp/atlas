# Project Atlas — Industrial Operations Console Design

## Direction

Project Atlas will use an **Industrial Operations Console** visual direction: a dark navy operations surface, amber and teal signals, high information density, explicit status labels, and clear separation between live operations and administrative controls. The interface is designed for an operator who keeps the console open while telemetry and alarms change continuously, while remaining polished enough for technical assessment review.

## Information architecture

The top-level shell will use a left sidebar with a persistent system identity, navigation groups, and a compact system-health footer. The primary group exposes Overview, Devices, Alarms, and Event History. The administration group is visible only to administrators and exposes Users and Audit Log. Device details remain a focused workspace with trend analysis, live status, configuration, thresholds, and a controlled remove action.

## Overview screen

The overview will show a high-level health strip for total devices, online devices, active critical alarms, and ingestion state. The main body will pair a device health table with a recent critical activity feed. A secondary telemetry summary will show the latest readings and their freshness without pretending to be a full chart. All values will be obtained from existing API endpoints; the screen will degrade gracefully when a service is unavailable.

## Device workspace

The device workspace will preserve the current trend chart and configuration downlink, then add threshold editing, connection freshness, broker provisioning state, and administrator-only removal. Operator actions will show explicit success or delivery failure states. Threshold controls will expose min/max, rate-of-change, statistical window, sigma, and confirmation samples in a compact form.

## Administration

The Users screen will list accounts, role, activity state, and creation time, with an administrator-only create-user form and deactivate action. The Audit screen will provide action/entity filters and a paginated read-only table displaying actor, action, entity, before/after state, IP, and timestamp. No screen will expose passwords or secrets.

## Visual system

Use CSS variables for surfaces, borders, text hierarchy, amber warnings, teal healthy state, red critical state, and blue interactive focus. Use restrained motion for shell entry, status changes, and hover feedback. Tables remain readable on narrow screens with horizontal overflow; cards collapse to one column on mobile. Status is always written as text and never encoded by color alone.

## Delivery sequence

Implement the shell and navigation first, then Overview, Users, Audit, threshold controls, device lifecycle controls, and finally visual refinement of existing Devices, Alarms, Events, and Login screens. Run dashboard lint/build after each slice and verify each new screen against its API contract before packaging the update.
