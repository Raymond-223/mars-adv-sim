"""MQTT request/reply adapter for real remote Agent planning.

Remote Agents propose ToolCall objects; ToolRuntime remains the only execution
boundary, so MQTT does not bypass permissions, idempotency, evidence, or events.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from ..models import Assignment, TaskNodeView, ToolCall


class AgentRpcClient(Protocol):
    def request(self, agent_id: str, message: Mapping[str, Any], timeout_s: float) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


class PahoMqttRpcClient:
    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        client_id: str | None = None,
        topic_prefix: str = "mosaic/v3",
        keepalive: int = 30,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
    ) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("MQTT Agent support requires paho-mqtt>=2") from exc
        self._mqtt = mqtt
        self.client_id = client_id or f"orchestrator-{uuid.uuid4().hex[:10]}"
        self.topic_prefix = topic_prefix.rstrip("/")
        self.response_topic = f"{self.topic_prefix}/orchestrators/{self.client_id}/plan/response"
        self._pending: dict[str, queue.Queue[Mapping[str, Any]]] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
        if username:
            self._client.username_pw_set(username, password=password)
        if tls:
            self._client.tls_set()

        def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
            try:
                ok = int(reason_code) == 0
            except Exception:
                ok = getattr(reason_code, "value", 1) == 0
            if ok:
                client.subscribe(self.response_topic, qos=1)
                self._connected.set()

        def on_disconnect(client: Any, userdata: Any, reason_code: Any, properties: Any = None) -> None:
            self._connected.clear()

        def on_message(client: Any, userdata: Any, msg: Any) -> None:
            try:
                raw = json.loads(msg.payload.decode("utf-8"))
                correlation_id = str(raw.get("correlation_id", ""))
            except Exception:
                return
            with self._lock:
                waiter = self._pending.get(correlation_id)
            if waiter is not None:
                waiter.put(raw)

        self._client.on_connect = on_connect
        self._client.on_disconnect = on_disconnect
        self._client.on_message = on_message
        self._client.reconnect_delay_set(1, 10)
        self._client.connect(host, int(port), keepalive=keepalive)
        self._client.loop_start()
        if not self._connected.wait(timeout=10):
            self.close()
            raise TimeoutError(f"MQTT broker connection timed out: {host}:{port}")

    def request(self, agent_id: str, message: Mapping[str, Any], timeout_s: float) -> Mapping[str, Any]:
        correlation_id = str(message.get("correlation_id") or f"rpc_{uuid.uuid4().hex}")
        payload = dict(message)
        payload["correlation_id"] = correlation_id
        payload["reply_to"] = self.response_topic
        waiter: queue.Queue[Mapping[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[correlation_id] = waiter
        try:
            topic = f"{self.topic_prefix}/agents/{agent_id}/plan/request"
            info = self._client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
            if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"MQTT publish failed rc={info.rc}")
            try:
                response = waiter.get(timeout=timeout_s)
            except queue.Empty as exc:
                raise TimeoutError(f"MQTT Agent plan timeout: {agent_id}") from exc
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return response
        finally:
            with self._lock:
                self._pending.pop(correlation_id, None)

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()


class MqttAgentAdapter:
    """Synchronous Orchestrator adapter backed by a real MQTT Agent process."""

    authenticity_mode = "remote_rpc"

    def __init__(self, actor_id: str, rpc: AgentRpcClient, *, timeout_s: float = 20.0) -> None:
        self.actor_id = actor_id
        self.rpc = rpc
        self.timeout_s = float(timeout_s)

    def plan(self, task: TaskNodeView, assignment: Assignment, trace_id: str) -> list[ToolCall]:
        correlation_id = f"rpc_{uuid.uuid4().hex}"
        request = {
            "type": "PLAN_REQUEST",
            "schema_version": assignment.schema_version,
            "correlation_id": correlation_id,
            "run_id": task.run_id,
            "task_id": task.task_id,
            "node_id": task.task_id,
            "trace_id": trace_id,
            "parent_event_id": None,
            "actor_id": "orchestrator",
            "model_id": assignment.model_id,
            "timestamp": time.time(),
            "payload": {
                "task": task.to_dict(),
                "assignment": assignment.to_dict(),
            },
        }
        response = self.rpc.request(self.actor_id, request, self.timeout_s)
        calls_raw = response.get("tool_calls", response.get("payload", {}).get("tool_calls", []))
        if not isinstance(calls_raw, list):
            raise ValueError("MQTT Agent response tool_calls must be a list")
        calls: list[ToolCall] = []
        for index, raw in enumerate(calls_raw):
            if not isinstance(raw, Mapping):
                raise ValueError("MQTT Agent ToolCall must be an object")
            normalized = dict(raw)
            normalized.update({
                "run_id": task.run_id,
                "task_id": task.task_id,
                "actor_id": self.actor_id,
                "trace_id": trace_id,
                "model_id": assignment.model_id,
                "schema_version": assignment.schema_version,
            })
            normalized.setdefault(
                "idempotency_key",
                f"{task.run_id}:{task.task_id}:{max(1, task.attempt)}:mqtt:{index}",
            )
            calls.append(ToolCall.from_dict(normalized))
        return calls
