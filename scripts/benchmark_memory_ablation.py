# -*- coding: utf-8 -*-
"""Controlled memory ablation using MOSAIC's real ContextBuilder.

Compares No Memory, Full History and ContextPack with the same synthetic history.
Token counts are tokenizer-free *estimates* from memory_recovery.estimate_tokens and
are never presented as provider/API token accounting.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mosaic_omega.memory_recovery.config import load_config
from mosaic_omega.memory_recovery.context_builder import ContextBuilder, estimate_tokens
from mosaic_omega.memory_recovery.models import MemoryRecord, MemoryType, VerificationStatus
from mosaic_omega.memory_recovery.repository import InMemoryRepository
from mosaic_omega.memory_recovery.retriever import Retriever
from mosaic_omega.memory_recovery.vector_index import VectorIndex
from mosaic_omega.memory_recovery.working_memory import WorkingMemory

RUN_ID = "memory-ablation-controlled"
NODE_ID = "T-final"
TASK_ID = "mission"

KEY_FACTS = [
    "最终交付必须包含 evidence SHA256",
    "禁止把估算 token 冒充真实 API token",
    "restricted 数据不得离开 DEVICE",
    "Agent 自报完成不能作为最终验收",
]


def _record(repo: InMemoryRepository, vector: VectorIndex, *, memory_type: MemoryType, content: str, summary: str, tags: list[str] | None = None, node_id: str = NODE_ID, importance: float = .5) -> None:
    item = MemoryRecord(
        run_id=RUN_ID,
        task_id=TASK_ID,
        node_id=node_id,
        memory_type=memory_type,
        content=content,
        summary=summary,
        importance=importance,
        confidence=.98,
        source="controlled_ablation_fixture",
        verification_status=VerificationStatus.VERIFIED,
        tags=tags or [],
        access_scope=["judge", "default"],
    )
    repo.save(item)
    vector.upsert(item)


def _build_fixture() -> tuple[InMemoryRepository, WorkingMemory, VectorIndex, list[str]]:
    cfg = replace(
        load_config(),
        recall_limit=18,
        context_pack_max_chars=6000,
        context_pack_max_tokens=2200,
        context_pack_max_facts=10,
        context_pack_max_experiences=6,
        context_pack_max_procedures=4,
        max_working_items=20,
        vector_candidate_limit=30,
    )
    repo = InMemoryRepository()
    working = WorkingMemory(cfg)
    vector = VectorIndex()
    working.set_state(
        RUN_ID,
        NODE_ID,
        current_goal="完成超长程复杂任务并形成可核验交付物",
        active_constraints=[KEY_FACTS[0], KEY_FACTS[2]],
        recent_results=["T58 verified", "T59 recovered", "T60 ready"],
        required_evidence=["EV-final"],
        current_agent="agent-verifier",
    )

    # Safety-critical facts are encoded through the production memory contract.
    _record(repo, vector, memory_type=MemoryType.SEMANTIC, content=KEY_FACTS[0], summary="evidence integrity", tags=["hard_constraint"], importance=1.0)
    _record(repo, vector, memory_type=MemoryType.SEMANTIC, content=KEY_FACTS[1], summary="token metric truth", tags=["prohibition"], importance=1.0)
    _record(repo, vector, memory_type=MemoryType.SEMANTIC, content=KEY_FACTS[2], summary="privacy placement", tags=["hard_constraint"], importance=1.0)
    _record(repo, vector, memory_type=MemoryType.SEMANTIC, content=KEY_FACTS[3], summary="independent verification", tags=["hard_constraint"], importance=1.0)

    # Long, repetitive history models the attention-dilution problem without an API call.
    full_history_lines: list[str] = []
    for step in range(1, 181):
        full_history_lines.append(
            f"step={step:03d} status=progress observation=重复运行信息{step % 9} "
            f"agent_note=继续执行计划并检查中间状态；普通上下文片段用于构造长历史。"
        )
        if step % 15 == 0:
            _record(
                repo, vector,
                memory_type=MemoryType.EPISODIC,
                content=f"step {step} transient progress with repeated observation {step % 9}",
                summary=f"episode {step}",
                node_id=f"T{step:03d}",
                importance=.35,
            )
        if step % 30 == 0:
            _record(
                repo, vector,
                memory_type=MemoryType.PROCEDURAL,
                content="1. 读取 Evidence 2. 检查哈希 3. 独立判定 4. 失败则恢复",
                summary="verification recovery procedure",
                node_id=f"T{step:03d}",
                importance=.75,
            )
    full_history_lines.extend(KEY_FACTS)
    return repo, working, vector, full_history_lines


def _recall(text: str) -> dict[str, Any]:
    hits = [fact for fact in KEY_FACTS if fact in text]
    return {
        "key_fact_hits": len(hits),
        "key_fact_total": len(KEY_FACTS),
        "key_fact_recall_pct": round(len(hits) / len(KEY_FACTS) * 100, 2),
        "recalled_facts": hits,
    }


def run_memory_ablation(workspace_dir: str, output_path: str | None = None) -> dict[str, Any]:
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    repo, working, vector, history_lines = _build_fixture()
    cfg = working.config
    retriever = Retriever(repo, working, vector_index=vector, config=cfg)
    builder = ContextBuilder(retriever, cfg)

    # Same final task instruction in all three arms.
    task_prompt = "请依据已有运行信息完成最终审核，确认隐私、证据、token 指标与独立验收要求。"
    no_memory_text = task_prompt
    full_history_text = task_prompt + "\n" + "\n".join(history_lines)

    started = time.perf_counter()
    pack = builder.build(
        run_id=RUN_ID,
        node_id=NODE_ID,
        task_id=TASK_ID,
        taskgraph_nodes=[NODE_ID] + [f"T{x:03d}" for x in range(150, 181)],
        query="最终审核 evidence token restricted DEVICE independent verification",
        evidence_ids=["EV-final"],
        allowed_scopes=["default", "judge"],
    )
    build_ms = (time.perf_counter() - started) * 1000.0
    context_pack_text = json.dumps(pack.to_dict(), ensure_ascii=False, separators=(",", ":"))

    modes: dict[str, dict[str, Any]] = {}
    for name, text in (
        ("no_memory", no_memory_text),
        ("full_history", full_history_text),
        ("context_pack", task_prompt + "\n" + context_pack_text),
    ):
        modes[name] = {
            "estimated_tokens": estimate_tokens(text),
            "estimated_chars": len(text),
            "provider_api_input_tokens": None,
            "provider_api_token_measurement": "not_measured_no_provider_call",
            **_recall(text),
        }
    modes["context_pack"]["context_builder_runtime_ms_measured"] = round(build_ms, 4)
    modes["context_pack"]["context_pack_truncated"] = bool(pack.truncated)
    modes["context_pack"]["memory_ids_selected"] = len(pack.memory_ids)

    full = max(1, int(modes["full_history"]["estimated_tokens"]))
    compact = int(modes["context_pack"]["estimated_tokens"])
    result = {
        "study_name": "MOSAIC-Ω memory controlled ablation",
        "measurement_mode": "production ContextBuilder + tokenizer-free estimated token budget; no provider API",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "controlled_variables": {
            "same_final_task_prompt": True,
            "same_key_facts": KEY_FACTS,
            "synthetic_history_steps": 180,
            "context_pack_limits": {
                "max_chars": cfg.context_pack_max_chars,
                "max_tokens": cfg.context_pack_max_tokens,
                "recall_limit": cfg.recall_limit,
            },
        },
        "modes": modes,
        "context_reduction_vs_full_history_pct_estimated": round((full - compact) / full * 100, 2),
        "truth_notes": [
            "estimated_tokens uses mosaic_omega.memory_recovery.context_builder.estimate_tokens; it is not tokenizer/provider billing data.",
            "provider_api_input_tokens is null because this controlled experiment makes no provider API request.",
            "Key-fact recall is an exact-string deterministic check over the same four required facts.",
            "Full History and ContextPack are compared on the same final task prompt and same generated history fixture.",
        ],
    }
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = __import__("argparse").ArgumentParser(description="Run memory ablation.")
    parser.add_argument("--workspace", default=".memory_ablation_workspace")
    parser.add_argument("--output", default=str(ROOT / "experiments" / "results" / "memory_ablation_v1.9.0.json"))
    args = parser.parse_args()
    result = run_memory_ablation(args.workspace, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
