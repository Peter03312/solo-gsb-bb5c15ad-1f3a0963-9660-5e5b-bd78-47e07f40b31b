"""告警确认测试：纯逻辑单测 + 端到端语义参考实现（仅依赖标准库）。

MemDB 复刻 app/api.py 中 create_acknowledgment 的判定顺序：
司机存在性 -> 实时重算违规存在性 -> 修订集合指纹幂等 -> 创建记录；
失效判断在查询时按现存记录实时比对（与 _ack_out 一致）。

可用 `python3 -m unittest tests.test_acknowledgment` 或 pytest 运行。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.acknowledgment import (
    diff_revisions,
    invalidation_reason,
    revision_set,
    revisions_fingerprint,
)
from app.compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H, Seg, evaluate_driver

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def t(h, m=0):
    return BASE + timedelta(hours=h, minutes=m)


# ------------------------------------------------------------ 纯逻辑


class RevisionSetTest(unittest.TestCase):
    def test_sorted_by_segment_id(self):
        revs = revision_set([("SEG-B", 2), ("SEG-A", 1)])
        self.assertEqual(
            revs,
            [{"segment_id": "SEG-A", "revision": 1}, {"segment_id": "SEG-B", "revision": 2}],
        )

    def test_fingerprint_order_independent(self):
        a = revision_set([("SEG-B", 2), ("SEG-A", 1)])
        b = revision_set([("SEG-A", 1), ("SEG-B", 2)])
        self.assertEqual(revisions_fingerprint(a), revisions_fingerprint(b))

    def test_fingerprint_changes_with_content(self):
        a = revision_set([("SEG-A", 1)])
        b = revision_set([("SEG-A", 2)])
        c = revision_set([("SEG-A", 1), ("SEG-B", 1)])
        self.assertNotEqual(revisions_fingerprint(a), revisions_fingerprint(b))
        self.assertNotEqual(revisions_fingerprint(a), revisions_fingerprint(c))


class InvalidationReasonTest(unittest.TestCase):
    def test_identical_sets_are_valid(self):
        saved = revision_set([("A", 1), ("B", 2)])
        self.assertIsNone(invalidation_reason(saved, revision_set([("B", 2), ("A", 1)])))

    def test_revision_changed(self):
        saved = revision_set([("A", 1), ("B", 1)])
        reason = invalidation_reason(saved, revision_set([("A", 2), ("B", 1)]))
        self.assertEqual(reason["code"], "segment_revision_changed")
        self.assertIn("A", reason["message"])
        self.assertEqual(
            reason["changes"]["revision_changed"],
            [{"segment_id": "A", "acknowledged_revision": 1, "current_revision": 2}],
        )

    def test_segment_added(self):
        reason = invalidation_reason(revision_set([("A", 1)]), revision_set([("A", 1), ("B", 1)]))
        self.assertEqual(reason["code"], "segment_added")
        self.assertEqual(reason["changes"]["added"], ["B"])

    def test_segment_removed(self):
        reason = invalidation_reason(revision_set([("A", 1), ("B", 1)]), revision_set([("A", 1)]))
        self.assertEqual(reason["code"], "segment_removed")
        self.assertEqual(reason["changes"]["removed"], ["B"])

    def test_mixed_changes(self):
        saved = revision_set([("A", 1), ("B", 1)])
        current = revision_set([("A", 2), ("C", 1)])
        reason = invalidation_reason(saved, current)
        self.assertEqual(reason["code"], "segments_changed")
        self.assertEqual(reason["changes"]["added"], ["C"])
        self.assertEqual(reason["changes"]["removed"], ["B"])
        self.assertEqual(len(reason["changes"]["revision_changed"]), 1)

    def test_diff_revisions_keys(self):
        changes = diff_revisions(revision_set([("A", 1)]), revision_set([("A", 1)]))
        self.assertEqual(changes, {"added": [], "removed": [], "revision_changed": []})


# ------------------------------------------------------------ 端到端语义参考实现


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MemDB:
    """内存表，复刻 API 处理器的判定顺序（全局互斥对应 pg_advisory_xact_lock）。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.acks: dict[int, dict] = {}
        self._ack_seq = 0

    def add_driver(self, d):
        self.drivers[d] = {"driver_id": d, "name": d}

    def add_segment(self, sid, driver, sh, eh, typ="DRIVING"):
        self.segments[sid] = {
            "segment_id": sid,
            "driver_id": driver,
            "start": t(sh),
            "end": t(eh),
            "type": typ,
            "revision": 1,
        }

    def patch_segment(self, sid, **fields):
        seg = self.segments[sid]
        seg.update(fields)
        seg["revision"] += 1

    def remove_segment(self, sid):
        del self.segments[sid]

    def driver_segments(self, driver):
        return sorted(
            (s for s in self.segments.values() if s["driver_id"] == driver),
            key=lambda s: (s["start"], s["end"], s["segment_id"]),
        )

    # ---- 与 app/api.py create_acknowledgment 判定顺序一致 ----

    def create_ack(self, driver_id, rule, note) -> dict:
        if driver_id not in self.drivers:
            raise HttpError(404, {"error": "driver_not_found", "driver_id": driver_id})

        result = evaluate_driver(
            Seg(s["segment_id"], s["start"], s["end"], s["type"])
            for s in self.driver_segments(driver_id)
        )
        violation = next((v for v in result["violations"] if v["rule"] == rule), None)
        if violation is None:
            raise HttpError(409, {"error": "violation_not_active", "rule": rule})

        revisions = revision_set(
            (s["segment_id"], s["revision"]) for s in self.driver_segments(driver_id)
        )
        fingerprint = revisions_fingerprint(revisions)

        for ack in self.acks.values():
            if (
                ack["driver_id"] == driver_id
                and ack["rule"] == rule
                and ack["fingerprint"] == fingerprint
            ):
                # 同司机同规则同修订集合：返回原记录，备注不同也不新增
                return {"created_new": False, "acknowledgment": self._ack_out(ack)}

        self._ack_seq += 1
        ack = {
            "ack_id": self._ack_seq,
            "driver_id": driver_id,
            "rule": rule,
            "note": note,
            "fingerprint": fingerprint,
            "segment_revisions": revisions,
            "violation": violation,
        }
        self.acks[ack["ack_id"]] = ack
        return {"created_new": True, "acknowledgment": self._ack_out(ack)}

    def _ack_out(self, ack) -> dict:
        # 与 app/api.py _ack_out 一致：失效状态按现存记录实时比对
        current = revision_set(
            (s["segment_id"], s["revision"]) for s in self.driver_segments(ack["driver_id"])
        )
        reason = invalidation_reason(ack["segment_revisions"], current)
        return {
            **ack,
            "valid": reason is None,
            "invalidation_reason": reason,
        }

    def list_acks(self, driver_id) -> list[dict]:
        if driver_id not in self.drivers:
            raise HttpError(404, {"error": "driver_not_found", "driver_id": driver_id})
        return [self._ack_out(a) for a in self.acks.values() if a["driver_id"] == driver_id]


def seed_violating_driver(db: MemDB, driver="D1"):
    """驾驶 08:00-12:30（中间 20 分钟休息不足 30 分钟，不清零）-> 累计驾驶越限。"""
    db.add_driver(driver)
    db.add_segment("SEG-A", driver, 0, 2, "DRIVING")
    db.add_segment("SEG-R", driver, 2, 2 + 20 / 60, "REST")
    db.add_segment("SEG-B", driver, 2 + 20 / 60, 4.5, "DRIVING")


class AcknowledgmentEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        seed_violating_driver(self.db)

    def test_driver_not_found(self):
        with self.assertRaises(HttpError) as ctx:
            self.db.create_ack("GHOST", RULE_DRIVING_STINT, "备注")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["error"], "driver_not_found")

    def test_violation_not_active(self):
        # 当前实时结果中没有滚动 24h 违规（只有累计驾驶违规）
        with self.assertRaises(HttpError) as ctx:
            self.db.create_ack("D1", RULE_ROLLING_24H, "备注")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "violation_not_active")

    def test_compliant_driver_cannot_ack(self):
        self.db.add_driver("D2")
        self.db.add_segment("SEG-OK", "D2", 0, 2, "DRIVING")
        with self.assertRaises(HttpError) as ctx:
            self.db.create_ack("D2", RULE_DRIVING_STINT, "备注")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_acknowledgment_stores_revision_set_and_violation(self):
        res = self.db.create_ack("D1", RULE_DRIVING_STINT, "已通知司机")
        self.assertTrue(res["created_new"])
        ack = res["acknowledgment"]
        self.assertTrue(ack["valid"])
        self.assertIsNone(ack["invalidation_reason"])
        self.assertEqual(ack["note"], "已通知司机")
        self.assertEqual(
            ack["segment_revisions"],
            revision_set([("SEG-A", 1), ("SEG-R", 1), ("SEG-B", 1)]),
        )
        self.assertEqual(ack["violation"]["rule"], RULE_DRIVING_STINT)
        self.assertEqual(ack["violation"]["segment_ids"], ["SEG-A", "SEG-B"])

    def test_idempotent_retry_returns_original_even_with_different_note(self):
        r1 = self.db.create_ack("D1", RULE_DRIVING_STINT, "首次备注")
        r2 = self.db.create_ack("D1", RULE_DRIVING_STINT, "另一个备注")
        self.assertTrue(r1["created_new"])
        self.assertFalse(r2["created_new"])
        self.assertEqual(r2["acknowledgment"]["ack_id"], r1["acknowledgment"]["ack_id"])
        # 备注不同也不新增、不更新
        self.assertEqual(r2["acknowledgment"]["note"], "首次备注")
        self.assertEqual(len(self.db.acks), 1)

    def test_revision_change_invalidates_with_reason(self):
        res = self.db.create_ack("D1", RULE_DRIVING_STINT, "备注")
        ack_id = res["acknowledgment"]["ack_id"]
        self.db.patch_segment("SEG-B", end=t(5))
        acks = self.db.list_acks("D1")
        self.assertEqual(len(acks), 1)
        ack = acks[0]
        self.assertEqual(ack["ack_id"], ack_id)
        self.assertFalse(ack["valid"])
        reason = ack["invalidation_reason"]
        self.assertEqual(reason["code"], "segment_revision_changed")
        self.assertEqual(
            reason["changes"]["revision_changed"],
            [{"segment_id": "SEG-B", "acknowledged_revision": 1, "current_revision": 2}],
        )

    def test_added_segment_invalidates(self):
        self.db.create_ack("D1", RULE_DRIVING_STINT, "备注")
        self.db.add_segment("SEG-C", "D1", 6, 7, "OTHER_DUTY")
        ack = self.db.list_acks("D1")[0]
        self.assertFalse(ack["valid"])
        self.assertEqual(ack["invalidation_reason"]["code"], "segment_added")
        self.assertEqual(ack["invalidation_reason"]["changes"]["added"], ["SEG-C"])

    def test_removed_segment_invalidates(self):
        self.db.create_ack("D1", RULE_DRIVING_STINT, "备注")
        self.db.remove_segment("SEG-R")
        ack = self.db.list_acks("D1")[0]
        self.assertFalse(ack["valid"])
        self.assertEqual(ack["invalidation_reason"]["code"], "segment_removed")
        self.assertEqual(ack["invalidation_reason"]["changes"]["removed"], ["SEG-R"])

    def test_mixed_changes_invalidate(self):
        self.db.create_ack("D1", RULE_DRIVING_STINT, "备注")
        self.db.patch_segment("SEG-A", end=t(2.5))
        self.db.add_segment("SEG-C", "D1", 6, 7, "REST")
        self.db.remove_segment("SEG-R")
        reason = self.db.list_acks("D1")[0]["invalidation_reason"]
        self.assertEqual(reason["code"], "segments_changed")
        self.assertEqual(reason["changes"]["added"], ["SEG-C"])
        self.assertEqual(reason["changes"]["removed"], ["SEG-R"])
        self.assertEqual(len(reason["changes"]["revision_changed"]), 1)

    def test_resubmit_after_change_creates_new_record(self):
        # 修订片段（违规仍存在）后重新提交：修订集合已变 -> 新记录；旧记录失效、新记录有效
        r1 = self.db.create_ack("D1", RULE_DRIVING_STINT, "首次")
        self.db.patch_segment("SEG-B", end=t(5))  # 仍越限（累计驾驶 4h40m）
        r2 = self.db.create_ack("D1", RULE_DRIVING_STINT, "再次确认")
        self.assertTrue(r2["created_new"])
        self.assertNotEqual(r2["acknowledgment"]["ack_id"], r1["acknowledgment"]["ack_id"])
        acks = self.db.list_acks("D1")
        self.assertEqual(len(acks), 2)
        by_id = {a["ack_id"]: a for a in acks}
        self.assertFalse(by_id[r1["acknowledgment"]["ack_id"]]["valid"])
        self.assertTrue(by_id[r2["acknowledgment"]["ack_id"]]["valid"])

    def test_ack_of_one_rule_does_not_affect_other_rule(self):
        # 制造两条规则同时违规：其他值勤 6h + 驾驶 5h（滚动 24h 与累计驾驶均越限）
        db = MemDB()
        db.add_driver("D3")
        db.add_segment("SEG-O", "D3", 0, 6, "OTHER_DUTY")
        db.add_segment("SEG-D", "D3", 6, 11, "DRIVING")
        db.create_ack("D3", RULE_ROLLING_24H, "确认滚动超限")
        db.create_ack("D3", RULE_DRIVING_STINT, "确认累计驾驶")
        self.assertEqual(len(db.acks), 2)  # 不同规则各自独立确认
        acks = db.list_acks("D3")
        self.assertTrue(all(a["valid"] for a in acks))
        self.assertEqual({a["rule"] for a in acks}, {RULE_ROLLING_24H, RULE_DRIVING_STINT})


if __name__ == "__main__":
    unittest.main()
