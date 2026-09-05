"""Measured reference split-inference runtime.

This is an executable reference MLP split across two Python processes. It proves
that MOSAIC can execute a model-stage boundary, serialize an activation tensor,
run the remainder remotely/process-isolated, and verify numerical equivalence.
It is intentionally labelled **reference MLP, not an LLM split**.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable


def _matrix(rows: int, cols: int, seed: int) -> list[list[float]]:
    return [[(((r + 1) * 17 + (c + 1) * 31 + seed * 13) % 37 - 18) / 23.0 for c in range(cols)] for r in range(rows)]


def _bias(size: int, seed: int) -> list[float]:
    return [(((i + 1) * 11 + seed * 7) % 19 - 9) / 41.0 for i in range(size)]


DIMS = (8, 16, 16, 8, 4)
WEIGHTS = tuple(_matrix(DIMS[i + 1], DIMS[i], i + 1) for i in range(len(DIMS) - 1))
BIASES = tuple(_bias(DIMS[i + 1], i + 1) for i in range(len(DIMS) - 1))
SPLIT_LAYER_INDEX = 2


def _layer(x: list[float], w: list[list[float]], b: list[float], *, relu: bool) -> list[float]:
    out = [sum(weight * value for weight, value in zip(row, x)) + bias for row, bias in zip(w, b)]
    return [max(0.0, value) for value in out] if relu else out


def run_layers(values: Iterable[float], start: int, end: int) -> list[float]:
    x = [float(v) for v in values]
    for index in range(start, end):
        x = _layer(x, WEIGHTS[index], BIASES[index], relu=index < len(WEIGHTS) - 1)
    return x


def monolithic_inference(values: Iterable[float]) -> list[float]:
    return run_layers(values, 0, len(WEIGHTS))


def cloud_stage(activation: Iterable[float]) -> list[float]:
    return run_layers(activation, SPLIT_LAYER_INDEX, len(WEIGHTS))


def run_pipeline_split(values: Iterable[float], *, python_executable: str | None = None) -> dict:
    input_vector = [float(v) for v in values]
    if len(input_vector) != DIMS[0]:
        raise ValueError(f"expected input dimension {DIMS[0]}")
    total_started = time.perf_counter()
    device_started = time.perf_counter()
    activation = run_layers(input_vector, 0, SPLIT_LAYER_INDEX)
    device_ms = (time.perf_counter() - device_started) * 1000.0
    payload = json.dumps({"activation": activation}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    cloud_started = time.perf_counter()
    # The worker must be runnable from a clean unpacked source tree too, not only
    # from an already-installed editable environment.  Explicitly propagate the
    # package ``src`` directory to the child rather than relying on the parent's
    # in-memory ``sys.path`` mutations.
    env = os.environ.copy()
    src_root = str(Path(__file__).resolve().parents[2])
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_root if not inherited else src_root + os.pathsep + inherited
    completed = subprocess.run(
        [python_executable or sys.executable, "-m", "mosaic_omega.agent_runtime.split_inference_worker"],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    cloud_wall_ms = (time.perf_counter() - cloud_started) * 1000.0
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))
    response = json.loads(completed.stdout.decode("utf-8"))
    split_output = [float(v) for v in response["output"]]
    monolithic = monolithic_inference(input_vector)
    max_abs_error = max((abs(a - b) for a, b in zip(split_output, monolithic)), default=0.0)
    total_ms = (time.perf_counter() - total_started) * 1000.0
    return {
        "benchmark": "split_inference_reference",
        "implementation": "actual_cross_process_reference_mlp",
        "claim_boundary": "REFERENCE_MLP_NOT_LLM_SPLIT",
        "partition_policy": "pipeline_split",
        "split_layer_index": SPLIT_LAYER_INDEX,
        "architecture_dims": list(DIMS),
        "device_stage_layers": [0, SPLIT_LAYER_INDEX - 1],
        "cloud_stage_layers": [SPLIT_LAYER_INDEX, len(WEIGHTS) - 1],
        "device_stage_ms_measured": device_ms,
        "activation_payload_bytes_measured": len(payload),
        "cloud_process_wall_ms_measured": cloud_wall_ms,
        "cloud_compute_ms_measured": float(response.get("cloud_compute_ms", 0.0)),
        "total_split_ms_measured": total_ms,
        "process_boundary_verified": bool(response.get("process_stage", False)),
        "monolithic_output": monolithic,
        "split_output": split_output,
        "max_abs_error": max_abs_error,
        "verified_equivalent": max_abs_error <= 1e-12,
        "measurement_semantics": "wall_clock_perf_counter_and_serialized_byte_length",
    }
