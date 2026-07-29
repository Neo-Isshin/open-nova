"""Deterministic memory governance signals for RAG v2.

This module is intentionally rule-based. It records source authority,
provenance completeness and lifecycle hints without calling an LLM or mutating
source material.
"""

from __future__ import annotations

from typing import Any


SOURCE_SET_POLICIES: dict[str, dict[str, Any]] = {
    "filtered-dialogue-daily": {
        "authorityRank": 78,
        "lifecycle": "episodic",
        "retention": "rolling-observation",
        "retrievalWeight": 0.99,
        "canonicalEligible": False,
    },
    "lessons": {
        "authorityRank": 95,
        "lifecycle": "canonical",
        "retention": "long-term",
        "retrievalWeight": 1.1,
        "canonicalEligible": True,
    },
    "foundation-usage-rollups": {
        "authorityRank": 82,
        "lifecycle": "metric",
        "retention": "materialized-fact",
        "retrievalWeight": 1.03,
        "canonicalEligible": True,
    },
    "foundation-dashboard-snapshots": {
        "authorityRank": 82,
        "lifecycle": "snapshot",
        "retention": "materialized-fact",
        "retrievalWeight": 1.03,
        "canonicalEligible": True,
    },
    "diary-markdown-sections": {
        "authorityRank": 90,
        "lifecycle": "narrative",
        "retention": "generated-report",
        "retrievalWeight": 1.06,
        "canonicalEligible": True,
    },
    "diary-markdown-embedded-json": {
        "authorityRank": 86,
        "lifecycle": "structured-report",
        "retention": "generated-report",
        "retrievalWeight": 1.03,
        "canonicalEligible": True,
    },
    "technical-report-task-events": {
        "authorityRank": 88,
        "lifecycle": "task-history",
        "retention": "historical-observation",
        "retrievalWeight": 1.06,
        "canonicalEligible": False,
    },
    "nova-task-work-graph-events": {
        "authorityRank": 91,
        "lifecycle": "work-graph",
        "retention": "current-state",
        "retrievalWeight": 1.08,
        "canonicalEligible": True,
    },
    "nova-task-reconciliation-events": {
        "authorityRank": 86,
        "lifecycle": "legacy-task-reconciliation",
        "retention": "historical-observation",
        "retrievalWeight": 1.02,
        "canonicalEligible": False,
    },
    "task-board-snapshot": {
        "authorityRank": 92,
        "lifecycle": "current-state",
        "retention": "current-state",
        "retrievalWeight": 1.08,
        "canonicalEligible": True,
    },
    "foundation-period-projections": {
        "authorityRank": 82,
        "lifecycle": "period-summary",
        "retention": "materialized-fact",
        "retrievalWeight": 1.03,
        "canonicalEligible": True,
    },
    "external-content": {
        "authorityRank": 55,
        "lifecycle": "operator-source",
        "retention": "operator-controlled",
        "retrievalWeight": 1.0,
        "canonicalEligible": False,
    },
    "agent-native-memory": {
        "authorityRank": 68,
        "lifecycle": "agent-native-memory",
        "retention": "agent-runtime-controlled",
        "retrievalWeight": 0.96,
        "canonicalEligible": False,
    },
    "agent-native-instructions": {
        "authorityRank": 74,
        "lifecycle": "agent-native-instructions",
        "retention": "agent-runtime-controlled",
        "retrievalWeight": 0.98,
        "canonicalEligible": False,
    },
}

DEFAULT_POLICY = {
    "authorityRank": 50,
    "lifecycle": "unknown",
    "retention": "operator-controlled",
    "retrievalWeight": 1.0,
    "canonicalEligible": False,
}


def governance_for_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    source_set = str(chunk.get("sourceSet") or "")
    policy = {**DEFAULT_POLICY, **SOURCE_SET_POLICIES.get(source_set, {})}
    provenance = chunk.get("provenance") if isinstance(chunk.get("provenance"), dict) else {}
    required = {
        "sourceSet": chunk.get("sourceSet"),
        "sourceId": chunk.get("sourceId"),
        "sourcePath": chunk.get("sourcePath"),
        "sourceType": chunk.get("sourceType"),
        "textHash": chunk.get("textHash"),
        "dedupeKey": chunk.get("dedupeKey"),
    }
    present = sum(1 for value in required.values() if value not in (None, ""))
    provenance_score = present / len(required)
    warnings = []
    if not chunk.get("date") and policy["lifecycle"] not in {"current-state", "unknown"}:
        warnings.append("missing-date")
    if not provenance:
        warnings.append("missing-provenance-detail")
    if source_set not in SOURCE_SET_POLICIES:
        warnings.append("unknown-source-set")
    retrieval_weight = float(policy["retrievalWeight"])
    if provenance_score < 0.75:
        retrieval_weight *= 0.95
    lifecycle = str(policy["lifecycle"])
    return {
        "version": 1,
        "sourceSet": source_set,
        "authorityRank": int(policy["authorityRank"]),
        "lifecycle": lifecycle,
        "retention": policy["retention"],
        "canonicalEligible": bool(policy["canonicalEligible"]),
        "provenanceScore": round(provenance_score, 4),
        "retrievalWeight": round(retrieval_weight, 4),
        "duplicateGroupKey": duplicate_group_key(chunk),
        "canonicalCandidate": bool(policy["canonicalEligible"] and provenance_score >= 0.75),
        "supersessionScope": supersession_scope(chunk, lifecycle=lifecycle),
        "warnings": warnings,
    }


def governance_for_source(source_set: str) -> dict[str, Any]:
    policy = {**DEFAULT_POLICY, **SOURCE_SET_POLICIES.get(str(source_set or ""), {})}
    return {
        "version": 1,
        "sourceSet": source_set,
        "authorityRank": int(policy["authorityRank"]),
        "lifecycle": policy["lifecycle"],
        "retention": policy["retention"],
        "canonicalEligible": bool(policy["canonicalEligible"]),
        "retrievalWeight": float(policy["retrievalWeight"]),
    }


def duplicate_group_key(chunk: dict[str, Any]) -> str:
    source_set = str(chunk.get("sourceSet") or "unknown")
    project = str(chunk.get("project") or "")
    agent = str(chunk.get("agent") or chunk.get("role") or "")
    text_hash = str(chunk.get("textHash") or "")
    if text_hash:
        return f"{source_set}:{project}:{agent}:{text_hash[:16]}"
    dedupe = str(chunk.get("dedupeKey") or "")
    return f"{source_set}:{project}:{agent}:{dedupe[:16]}"


def supersession_scope(chunk: dict[str, Any], *, lifecycle: str | None = None) -> str | None:
    selected = lifecycle or str((chunk.get("governance") or {}).get("lifecycle") or "")
    if selected == "current-state":
        project = str(chunk.get("project") or "global")
        work_type = str(chunk.get("workType") or "state")
        return f"current-state:{project}:{work_type}"
    if selected in {"task-history", "period-summary", "metric", "snapshot"}:
        source_set = str(chunk.get("sourceSet") or "unknown")
        project = str(chunk.get("project") or "global")
        date = str(chunk.get("date") or "")[:10]
        return f"{source_set}:{project}:{date}"
    return None
