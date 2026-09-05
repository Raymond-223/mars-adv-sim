from __future__ import annotations

import argparse
import os
from typing import Any, Mapping

from mosaic_omega.execution_scheduler.adapters.mqtt_worker import PahoPlannerWorker


def planner(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payload = dict(request.get("payload", {}))
    task = dict(payload.get("task", {}))
    assignment = dict(payload.get("assignment", {}))
    spec = dict(task.get("metadata", {}).get("tool", {}))
    tool_name = str(spec.get("name") or assignment.get("tool_id") or "task")
    arguments = dict(spec.get("arguments", {}))
    if tool_name == "task":
        arguments.setdefault("description", task.get("description", task.get("task_id", "task")))
        arguments.setdefault("acceptance_conditions", task.get("acceptance_conditions", []))
    return [{
        "tool_name": tool_name,
        "arguments": arguments,
        "required_permissions": task.get("required_permissions", []),
        "timeout_s": spec.get("timeout_s"),
        "idempotency_key": f"{request.get('run_id')}:{request.get('task_id')}:mqtt",
    }]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD", ""))
    parser.add_argument("--tls", action="store_true", default=os.getenv("MQTT_TLS", "false").casefold() in {"1","true","yes","on"})
    args = parser.parse_args()
    worker = PahoPlannerWorker(agent_id=args.agent_id, host=args.host, port=args.port, planner=planner, username=args.username or None, password=args.password or None, tls=args.tls)
    worker.run_forever()


if __name__ == "__main__":
    main()
