"""Dynamic task topology and receiver-aware low-entropy message primitives.

This package is deliberately independent from the MQTT adapter.  The authoritative
main chain calls it before an existing ``TaskMessage`` is delivered, so transport
can change without creating a second communication policy path.
"""

from .config import TopologyConfig
from .delta_builder import DeltaBuildResult, DeltaBuilder
from .knowledge_digest import KnowledgeDigestStore
from .models import (
    EdgeCandidate,
    EdgeState,
    KnowledgeDigest,
    RebuildResult,
    TopologySnapshot,
)
from .semantic_dedup import SemanticDeduplicator
from .service import ContextDeliveryPlan, MessageTopologyService
from .feedback import DecisionFeedbackEvent, DecisionFeedbackStore
from .telemetry import MessageTopologyTelemetry
from .topology_manager import TopologyManager

__all__ = [
    "DeltaBuildResult",
    "DeltaBuilder",
    "EdgeCandidate",
    "EdgeState",
    "KnowledgeDigest",
    "KnowledgeDigestStore",
    "MessageTopologyService",
    "DecisionFeedbackEvent",
    "DecisionFeedbackStore",
    "MessageTopologyTelemetry",
    "RebuildResult",
    "SemanticDeduplicator",
    "TopologyConfig",
    "TopologyManager",
    "TopologySnapshot",
]
