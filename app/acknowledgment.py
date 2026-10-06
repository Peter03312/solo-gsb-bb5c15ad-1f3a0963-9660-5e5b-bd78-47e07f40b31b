"""告警确认的纯逻辑（仅依赖标准库，可独立测试）。

确认记录在创建时固化“覆盖的片段修订集合”——提交时刻该司机全部现存片段的
``(segment_id, revision)`` 集合（按 segment_id 排序）及其 SHA-256 指纹：

- **幂等**：同一司机、同一规则且修订集合未变（指纹相同）时重复提交返回原记录，
  备注不同也不新增；
- **失效**：之后任一片段增删或修订（修订号变化）都会使当前修订集合与固化集合
  不再一致。失效判断在查询时按现存记录实时比对（不落库、无修改入口），
  并给出结构化的失效原因（新增 / 删除 / 修订号变化的片段明细）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

# 失效原因代码：单一诱因时给出具体代码，多种诱因同时存在时为 segments_changed
_CODE_BY_KIND = {
    "added": "segment_added",
    "removed": "segment_removed",
    "revision_changed": "segment_revision_changed",
}


def revision_set(segment_revisions: Iterable[tuple[str, int]]) -> list[dict]:
    """把 ``(segment_id, revision)`` 对规范化为按 segment_id 排序的修订集合。"""
    return [
        {"segment_id": segment_id, "revision": revision}
        for segment_id, revision in sorted(segment_revisions)
    ]


def revisions_fingerprint(revisions: list[dict]) -> str:
    """修订集合的 SHA-256 指纹；集合相同（与顺序无关）则指纹相同。"""
    payload = json.dumps(revisions, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_revisions(saved: list[dict], current: list[dict]) -> dict[str, Any]:
    """比对固化集合与当前集合，返回三类差异：新增 / 删除 / 修订号变化。"""
    saved_map = {r["segment_id"]: r["revision"] for r in saved}
    current_map = {r["segment_id"]: r["revision"] for r in current}
    return {
        "added": sorted(current_map.keys() - saved_map.keys()),
        "removed": sorted(saved_map.keys() - current_map.keys()),
        "revision_changed": [
            {
                "segment_id": sid,
                "acknowledged_revision": saved_map[sid],
                "current_revision": current_map[sid],
            }
            for sid in sorted(saved_map.keys() & current_map.keys())
            if saved_map[sid] != current_map[sid]
        ],
    }


def invalidation_reason(saved: list[dict], current: list[dict]) -> Optional[dict[str, Any]]:
    """任一片段增删或修订即失效：比对固化集合与当前集合。

    完全一致返回 ``None``（确认仍有效）；否则返回结构化失效原因，
    包含原因代码、人读信息与三类差异明细。
    """
    changes = diff_revisions(saved, current)
    kinds = [k for k in ("added", "removed", "revision_changed") if changes[k]]
    if not kinds:
        return None

    code = _CODE_BY_KIND[kinds[0]] if len(kinds) == 1 else "segments_changed"
    parts: list[str] = []
    if changes["added"]:
        parts.append("新增片段 " + "、".join(changes["added"]))
    if changes["removed"]:
        parts.append("片段已删除 " + "、".join(changes["removed"]))
    if changes["revision_changed"]:
        parts.append(
            "片段已修订 "
            + "、".join(
                f"{c['segment_id']}（修订号 {c['acknowledged_revision']} -> {c['current_revision']}）"
                for c in changes["revision_changed"]
            )
        )
    return {
        "code": code,
        "message": "；".join(parts) + "，原确认失效",
        "changes": changes,
    }
