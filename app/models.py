from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base

# PostgreSQL 上使用 JSONB，其他数据库（如本地 sqlite 测试）退化为普通 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")


class Driver(Base):
    __tablename__ = "drivers"

    driver_id = Column(String(64), primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Segment(Base):
    __tablename__ = "segments"

    segment_id = Column(String(64), primary_key=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    start_utc = Column(DateTime(timezone=True), nullable=False)
    end_utc = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(16), nullable=False)  # DRIVING / OTHER_DUTY / REST
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReassignmentBatch(Base):
    """跨司机批量调整的批次记录：批次号唯一，固化首次提交的结果。

    无论首次提交成功（status=2xx）还是被整批拒绝（status=4xx），都在此留痕：
    同批次号同内容重放 -> 返回首次结果；同批次号异内容 -> 409 冲突。
    """

    __tablename__ = "reassignment_batches"

    batch_id = Column(String(64), primary_key=True)
    content_fingerprint = Column(String(64), nullable=False)
    request_payload = Column(JSONType, nullable=False)  # 规范化后的批次内容
    status_code = Column(Integer, nullable=False)  # 首次提交的 HTTP 状态码
    response = Column(JSONType, nullable=False)  # 首次提交的响应体（成功/错误明细）
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Acknowledgment(Base):
    """告警确认：针对创建时刻实时结果中存在的某条违规的确认记录。

    创建时固化所覆盖的片段修订集合（当时该司机全部现存片段的
    (segment_id, revision) 集合）及其指纹；之后任一片段增删或修订
    都会使确认失效（查询时按现存记录实时比对，不落库、无修改入口）。
    (driver_id, rule, fingerprint) 唯一约束保证同司机同规则同修订集合
    的重复/并发提交只留一条记录。
    """

    __tablename__ = "acknowledgments"
    __table_args__ = (
        UniqueConstraint("driver_id", "rule", "fingerprint", name="uq_ack_driver_rule_fingerprint"),
    )

    ack_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    rule = Column(String(64), nullable=False)  # 违规规则标识
    note = Column(String(2000), nullable=False)  # 确认备注（重复提交不更新）
    fingerprint = Column(String(64), nullable=False)  # 提交时覆盖的修订集合指纹
    segment_revisions = Column(JSONType, nullable=False)  # [{"segment_id":..,"revision":..}, ...]
    violation = Column(JSONType, nullable=False)  # 确认针对的违规快照（规则/首次越限时刻/参与片段）
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditSnapshot(Base):
    """审核发布快照：固化发布时刻的输入修订与合规结论，之后更正不影响旧版。"""

    __tablename__ = "audit_snapshots"
    __table_args__ = (
        # 同一司机同一输入修订指纹只允许存在一版，并发发布由该唯一约束兜底
        UniqueConstraint("driver_id", "fingerprint", name="uq_audit_driver_fingerprint"),
    )

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False)
    input_revisions = Column(JSONType, nullable=False)  # [{"segment_id":..,"revision":..}, ...]
    result = Column(JSONType, nullable=False)  # evaluate_driver 的结论
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
