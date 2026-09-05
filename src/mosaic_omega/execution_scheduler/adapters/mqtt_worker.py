"""Companion MQTT worker for remote Agent planning."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

Planner = Callable[[Mapping[str, Any]], list[Mapping[str, Any]]]


class PahoPlannerWorker:
    def __init__(self, *, agent_id: str, host: str, planner: Planner, port: int = 1883, topic_prefix: str = "mosaic/v3", username: str | None = None, password: str | None = None, tls: bool = False) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("MQTT Agent support requires paho-mqtt>=2") from exc
        self.agent_id = agent_id
        self.planner = planner
        self.topic_prefix = topic_prefix.rstrip("/")
        self.request_topic = f"{self.topic_prefix}/agents/{agent_id}/plan/request"
        self.client = mqtt.Client(client_id=f"agent-{agent_id}", protocol=mqtt.MQTTv311)
        if username:
            self.client.username_pw_set(username, password=password)
        if tls:
            self.client.tls_set()

        def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
            client.subscribe(self.request_topic, qos=1)

        def on_message(client: Any, userdata: Any, msg: Any) -> None:
            request: dict[str, Any] = {}
            try:
                request = json.loads(msg.payload.decode("utf-8"))
                calls = self.planner(request)
                response = {
                    "type": "PLAN_RESPONSE",
                    "schema_version": request.get("schema_version", "0.1"),
                    "correlation_id": request["correlation_id"],
                    "run_id": request.get("run_id"),
                    "task_id": request.get("task_id"),
                    "trace_id": request.get("trace_id"),
                    "actor_id": self.agent_id,
                    "model_id": request.get("model_id"),
                    "timestamp": time.time(),
                    "tool_calls": calls,
                }
            except Exception as exc:
                response = {
                    "type": "PLAN_RESPONSE",
                    "correlation_id": request.get("correlation_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            reply_to = request.get("reply_to")
            if reply_to:
                client.publish(str(reply_to), json.dumps(response, ensure_ascii=False), qos=1)

        self.client.on_connect = on_connect
        self.client.on_message = on_message
        self.client.connect(host, int(port), keepalive=30)

    def run_forever(self) -> None:
        self.client.loop_forever(retry_first_connection=True)
