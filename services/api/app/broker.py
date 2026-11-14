"""
Project Atlas - broker credential provisioning
AT-26-L1-002  |  FR-25, FR-26  |  M3 Dynamic Security integration

The API provisions device credentials through the Mosquitto Dynamic Security
Plugin. Device passwords are sent to the broker and returned to the caller
once; they are never persisted by this service or written to logs.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .logging_setup import log


MODE = os.getenv("BROKER_PROVISIONING", "static").lower()
BROKER_HOST = os.getenv("BROKER_HOST", "atlas-broker")
BROKER_PORT = int(os.getenv("BROKER_PORT", "8883"))
BROKER_CA = os.getenv("BROKER_CA", "/certs/ca.crt")
BROKER_TLS_INSECURE = os.getenv("BROKER_TLS_INSECURE", "false").lower() in {
    "1", "true", "yes"
}
DYNSEC_ADMIN_USER = os.getenv("DYNSEC_ADMIN_USER", "")
DYNSEC_ADMIN_PASSWORD = os.getenv("DYNSEC_ADMIN_PASSWORD", "")
DYNSEC_TIMEOUT_SEC = float(os.getenv("DYNSEC_TIMEOUT_SEC", "5"))
BROKER_APP_USER = os.getenv("MQTT_API_USER", "")
BROKER_APP_PASSWORD = os.getenv("MQTT_API_PASSWORD", "")
BROKER_PUBLISH_TIMEOUT_SEC = float(os.getenv("BROKER_PUBLISH_TIMEOUT_SEC", "5"))

CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"
DEVICE_ROLE = "atlas-device"

_MANUAL_STEP = (
    "Broker provisioning is manual until the Dynamic Security Plugin migration "
    "(M3). Add the client with: docker compose exec broker mosquitto_passwd -b "
    "/mosquitto/config/passwd <username> <secret>, add the ACL line, then "
    "docker compose kill -s HUP broker."
)
_DYNSEC_FAILURE = "Dynamic Security broker provisioning failed."


class _DynsecError(RuntimeError):
    """An error returned by, or while contacting, the broker control API."""


class _DynsecRequest:
    """One synchronous request over the broker's Dynamic Security topic API."""

    def __init__(self, command: dict[str, Any]):
        self.command = command
        self.payload = json.dumps({"commands": [command]}, separators=(",", ":"))
        self.done = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: str | None = None

    @staticmethod
    def _failed_reason(reason: Any) -> bool:
        try:
            return bool(getattr(reason, "is_failure", False)) or int(reason) != 0
        except (TypeError, ValueError):
            return True

    def _fail(self, message: str) -> None:
        if not self.done.is_set():
            self.error = message[:300]
            self.done.set()

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any,
                    reason_code: Any, _properties: Any) -> None:
        if self._failed_reason(reason_code):
            self._fail(f"broker connection failed: {reason_code}")
            return

        result, _mid = client.subscribe(RESPONSE_TOPIC, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._fail(f"broker response subscription failed: {result}")

    def _on_subscribe(self, client: mqtt.Client, _userdata: Any, _mid: int,
                      reason_codes: list[Any], _properties: Any) -> None:
        if any(self._failed_reason(code) and int(code) >= 128
               for code in reason_codes):
            self._fail("broker rejected response subscription")
            return

        info = client.publish(CONTROL_TOPIC, self.payload, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self._fail(f"broker control publish failed: {info.rc}")

    def _on_message(self, _client: mqtt.Client, _userdata: Any,
                    message: mqtt.MQTTMessage) -> None:
        try:
            response = json.loads(message.payload.decode("utf-8"))
            responses = response.get("responses")
            if not isinstance(responses, list) or not responses:
                raise ValueError("broker response did not contain responses")
            first = responses[0]
            if not isinstance(first, dict):
                raise ValueError("broker response entry was not an object")
            self.response = first
            self.done.set()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._fail(f"invalid broker response: {exc}")

    def execute(self) -> dict[str, Any]:
        if not DYNSEC_ADMIN_USER or not DYNSEC_ADMIN_PASSWORD:
            raise _DynsecError(
                "DYNSEC_ADMIN_USER and DYNSEC_ADMIN_PASSWORD are required"
            )
        if not os.path.isfile(BROKER_CA):
            raise _DynsecError(f"broker CA file is missing: {BROKER_CA}")

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"atlas-api-dynsec-{uuid.uuid4().hex}",
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(DYNSEC_ADMIN_USER, DYNSEC_ADMIN_PASSWORD)
        client.tls_set(ca_certs=BROKER_CA)
        client.tls_insecure_set(BROKER_TLS_INSECURE)
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message

        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
            client.loop_start()
            if not self.done.wait(DYNSEC_TIMEOUT_SEC):
                raise _DynsecError("timed out waiting for broker response")
            if self.error:
                raise _DynsecError(self.error)
            if self.response is None:
                raise _DynsecError("broker returned no response")
            return self.response
        finally:
            try:
                client.disconnect()
            finally:
                client.loop_stop()


def _execute(command: dict[str, Any]) -> None:
    """Execute one command and convert a broker error into a safe exception."""
    response = _DynsecRequest(command).execute()
    if response.get("error"):
        # The broker's detail is logged for operators, but never sent back with
        # the generated password or included in an HTTP response.
        detail = str(response["error"])[:300]
        log("error", "dynamic security command rejected",
            command=command.get("command"), detail=detail)
        raise _DynsecError("dynamic security command rejected")


def provision_device(device_uid: str, secret: str) -> tuple[bool, str | None]:
    """Create the broker client and attach the least-privilege device role."""
    if MODE != "dynsec":
        log("warning", "device registered without broker provisioning",
            device_uid=device_uid, mode=MODE)
        return False, _MANUAL_STEP

    try:
        _execute({
            "command": "createClient",
            "username": device_uid,
            "password": secret,
            "roles": [{"rolename": DEVICE_ROLE, "priority": -1}],
        })
    except (OSError, _DynsecError, RuntimeError) as exc:
        log("error", "dynamic security provisioning failed",
            device_uid=device_uid, error=f"{type(exc).__name__}: {exc}"[:300])
        return False, _DYNSEC_FAILURE

    log("info", "device provisioned in broker", device_uid=device_uid)
    return True, None


def publish_device_config(device_uid: str, site: str,
                          sample_interval_ms: int,
                          enabled_metrics: list[str]) -> None:
    """Publish the retained device configuration through the application client.

    Retaining the latest configuration means a device that reconnects after a
    power loss receives the same desired state immediately after subscribing.
    The API's Dynamic Security admin is deliberately not used here: that account
    must remain limited to the broker control topic.
    """
    if not BROKER_APP_USER or not BROKER_APP_PASSWORD:
        raise _DynsecError("MQTT_API_USER and MQTT_API_PASSWORD are required")
    if not os.path.isfile(BROKER_CA):
        raise _DynsecError(f"broker CA file is missing: {BROKER_CA}")

    topic = f"atlas/{site}/{device_uid}/config"
    payload = json.dumps({
        "device_uid": device_uid,
        "sample_interval_ms": sample_interval_ms,
        "enabled_metrics": enabled_metrics,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    }, separators=(",", ":"))
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"atlas-api-config-{uuid.uuid4().hex}",
        protocol=mqtt.MQTTv5,
    )
    client.username_pw_set(BROKER_APP_USER, BROKER_APP_PASSWORD)
    client.tls_set(ca_certs=BROKER_CA)
    client.tls_insecure_set(BROKER_TLS_INSECURE)
    try:
        client.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
        client.loop_start()
        info = client.publish(topic, payload, qos=1, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise _DynsecError(f"configuration publish failed: {info.rc}")
        info.wait_for_publish(timeout=BROKER_PUBLISH_TIMEOUT_SEC)
        if not info.is_published():
            raise _DynsecError("configuration publish timed out")
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()


def deprovision_device(device_uid: str) -> tuple[bool, str | None]:
    """Delete the broker client so a removed device cannot reconnect."""
    if MODE != "dynsec":
        log("warning", "device removed from platform but broker client remains",
            device_uid=device_uid, mode=MODE)
        return False, (
            "Remove the broker client by hand: delete the line from "
            "infra/mosquitto/config/passwd and the matching ACL entry, "
            "then docker compose kill -s HUP broker."
        )

    try:
        _execute({"command": "deleteClient", "username": device_uid})
    except (OSError, _DynsecError, RuntimeError) as exc:
        log("error", "dynamic security deprovisioning failed",
            device_uid=device_uid, error=f"{type(exc).__name__}: {exc}"[:300])
        return False, _DYNSEC_FAILURE

    log("info", "device deprovisioned in broker", device_uid=device_uid)
    return True, None
