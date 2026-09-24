"""业务逻辑：事件入库、幂等去重、乱序后的最新状态查询、时钟回拨校正、超温积分。"""
import math
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _content_equal(existing: models.BoxEvent, payload: schemas.EventIn) -> bool:
    """内容是否一致（温度 + 事件时间）。

    event_time 在 schema 层已统一换算为 UTC naive，可直接比较；
    设备重试会原样重发 JSON，float 解析结果相同，故直接判等。
    """
    return (
        existing.temperature == payload.temperature
        and existing.event_time == payload.event_time
    )


def submit_event(db: Session, payload: schemas.EventIn) -> tuple[models.BoxEvent, bool]:
    """写入一个事件。

    返回 (事件行, 是否重复)。
    - 同箱号同序号且内容相同：视为设备重试/延迟的重复上报，幂等返回原行，不改状态
    - 同箱号同序号但内容不同：拒绝（409）
    - 新序号：直接入库（即使乱序，不要求 seq 连续）
    """
    existing = db.scalar(
        select(models.BoxEvent).where(
            models.BoxEvent.box_code == payload.box_code,
            models.BoxEvent.seq == payload.seq,
        )
    )

    if existing is not None:
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "seq_conflict",
                    "message": (
                        f"箱号 {payload.box_code} 的序号 {payload.seq} 已存在，"
                        "但温度/事件时间与本次上报不一致，已拒绝"
                    ),
                    "existing": {
                        "temperature": existing.temperature,
                        "event_time": existing.event_time.isoformat() + "Z",
                    },
                    "received": {
                        "temperature": payload.temperature,
                        "event_time": payload.event_time.isoformat() + "Z",
                    },
                },
            )
        return existing, True

    event = models.BoxEvent(
        box_code=payload.box_code,
        seq=payload.seq,
        temperature=payload.temperature,
        event_time=payload.event_time,
    )
    db.add(event)
    try:
        db.flush()  # 触发唯一约束，兜住并发下的竞态
    except IntegrityError:
        db.rollback()
        # 并发请求抢先写入：以库中记录为准重新判定
        existing = db.scalar(
            select(models.BoxEvent).where(
                models.BoxEvent.box_code == payload.box_code,
                models.BoxEvent.seq == payload.seq,
            )
        )
        if existing is None:  # 理论上不会发生
            raise
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "seq_conflict", "message": "同序号内容冲突，已拒绝"},
            )
        return existing, True

    db.commit()
    db.refresh(event)
    return event, False


def list_boxes(db: Session) -> list[str]:
    rows = db.scalars(
        select(models.BoxEvent.box_code)
        .distinct()
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def _latest_subquery():
    """每个箱号当前最大序号（即最新状态），乱序入库也不受影响。"""
    return (
        select(
            models.BoxEvent.box_code.label("box_code"),
            func.max(models.BoxEvent.seq).label("max_seq"),
        )
        .group_by(models.BoxEvent.box_code)
        .subquery()
    )


def latest_states(db: Session) -> list[models.BoxEvent]:
    latest = _latest_subquery()
    rows = db.scalars(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        )
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def latest_state(db: Session, box_code: str) -> models.BoxEvent | None:
    latest = _latest_subquery()
    return db.scalar(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        ).where(models.BoxEvent.box_code == box_code)
    )


def list_events(db: Session, box_code: str) -> list[models.BoxEvent]:
    return list(
        db.scalars(
            select(models.BoxEvent)
            .where(models.BoxEvent.box_code == box_code)
            .order_by(models.BoxEvent.seq)
        ).all()
    )


def _to_epoch_micros(dt: datetime) -> int:
    """UTC naive datetime -> 自 Unix 纪元的整数微秒（精确，无浮点误差）。"""
    delta = dt.replace(tzinfo=timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_epoch_micros(micros: int) -> datetime:
    """整数微秒 -> UTC naive datetime。"""
    secs, rem = divmod(micros, 1_000_000)
    return (_EPOCH + timedelta(seconds=secs, microseconds=rem)).replace(tzinfo=None)


def _isotonic_blocks(values: list[int]) -> list[tuple[int, int, int]]:
    """等权保序回归（PAVA，Pool Adjacent Violators Algorithm）。

    输入按 seq 升序的整数微秒事件时间；在“建议值非递减”约束下最小化
    sum((建议值 - 原值)^2)，等价于把相邻违规块不断合并、块内取算术均值。

    为避免大整数除法的浮点误差，块以 (序号起点, 序号终点, 总和, 个数)
    维护；块间比较与均值输出均用整数运算。均值四舍五入到微秒
    （半数远离零；微秒均值不可能出现半数情形时同样正确）。

    返回 [(index_from, index_to, rounded_mean_micros), ...]，
    下标对应输入 values 的位置，由调用方映射回事件 seq。
    """
    # 每个元素：[seq_from, seq_to, total_micros, count]
    blocks: list[list[int]] = []
    for i, v in enumerate(values):
        block = [i, i, v, 1]
        # 前一块均值 > 当前块均值即为违规，向前合并直到满足非递减
        while blocks:
            prev = blocks[-1]
            # prev.total / prev.count > block.total / block.count
            if prev[2] * block[3] <= block[2] * prev[3]:
                break
            blocks.pop()
            block = [
                prev[0],
                block[1],
                prev[2] + block[2],
                prev[3] + block[3],
            ]
        blocks.append(block)

    def _round_half_away(total: int, count: int) -> int:
        q, r = divmod(abs(total), count)
        # 余数 * 2 >= 分母即小数部分 >= 0.5，向远离零方向进位
        if 2 * r >= count:
            q += 1
        return q if total >= 0 else -q

    return [
        (b[0], b[1], _round_half_away(b[2], b[3])) for b in blocks
    ]


def _events_in_range(
    db: Session, box_code: str, seq_from: int, seq_to: int
) -> list[models.BoxEvent] | None:
    """按 seq 升序取区间内事件（含端点）。

    箱号不存在（从未上报过）返回 None，由调用方转 404；
    箱号存在但区间内无事件返回 []。缺号不补点，只返回库中实际事件。
    """
    box_exists = db.scalar(
        select(func.count())
        .select_from(models.BoxEvent)
        .where(models.BoxEvent.box_code == box_code)
    )
    if not box_exists:
        return None

    return list(
        db.scalars(
            select(models.BoxEvent)
            .where(
                models.BoxEvent.box_code == box_code,
                models.BoxEvent.seq >= seq_from,
                models.BoxEvent.seq <= seq_to,
            )
            .order_by(models.BoxEvent.seq)
        ).all()
    )


def _isotonic_suggestions(
    events: list[models.BoxEvent],
) -> tuple[list[int], list[tuple[int, int]]]:
    """对按 seq 升序的事件做等权保序回归校正（clock-audit 同款算法）。

    返回 (逐事件建议时间的整数微秒, 逐事件所属合并块的 (起始序号, 结束序号))，
    下标与 events 一一对应。只读计算，不改动任何原始记录。
    """
    values = [_to_epoch_micros(e.event_time) for e in events]
    blocks = _isotonic_blocks(values)

    # PAVA 输出的块下标对应 events 下标；展开为逐事件的建议
    suggested: list[int] = [0] * len(events)
    block_range: list[tuple[int, int]] = [(0, 0)] * len(events)
    for idx_from, idx_to, mean_micros in blocks:
        seq_from_block = events[idx_from].seq
        seq_to_block = events[idx_to].seq
        for i in range(idx_from, idx_to + 1):
            suggested[i] = mean_micros
            block_range[i] = (seq_from_block, seq_to_block)
    return suggested, block_range


def clock_audit(
    db: Session, box_code: str, seq_from: int, seq_to: int
) -> list[schemas.ClockAuditItem] | None:
    """对指定序号区间（含端点）生成设备时钟回拨校正建议。

    - 箱号不存在（从未上报过）：返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回 []
    - 校正只在本次查询中即时计算，不改动任何原始记录；乱序补报后再次查询
      会基于最新数据重新计算
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None
    if not events:
        return []

    values = [_to_epoch_micros(e.event_time) for e in events]
    suggested, block_range = _isotonic_suggestions(events)

    return [
        schemas.ClockAuditItem(
            seq=event.seq,
            original_time=event.event_time,
            suggested_time=_from_epoch_micros(sugg),
            offset_micros=sugg - values[i],
            block_seq_from=block_range[i][0],
            block_seq_to=block_range[i][1],
        )
        for i, (event, sugg) in enumerate(zip(events, suggested))
    ]


def _segment_exposure(
    temp_from: float, temp_to: float, upper_limit: float, gap_seconds: float
) -> float:
    """相邻两点间温度线性变化时，超过 upper_limit 部分对时间的精确积分（℃·秒）。

    超出量 a = temp_from - upper_limit、b = temp_to - upper_limit 同样线性变化，
    在时长 gap_seconds（> 0）上对 max(超出量, 0) 积分：
    - 两端都不超：0
    - 两端都超：梯形面积 (a + b) / 2 * d
    - 一端超：线性过限，超出部分是三角形，面积 超端超出量² / (2 * |b - a|) * d
    """
    a = temp_from - upper_limit
    b = temp_to - upper_limit
    if a <= 0.0 and b <= 0.0:
        return 0.0
    if a >= 0.0 and b >= 0.0:
        return (a + b) / 2.0 * gap_seconds
    over = max(a, b)
    return over * over / (2.0 * abs(b - a)) * gap_seconds


def heat_exposure(
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    upper_limit: float,
    max_gap_seconds: float,
) -> schemas.HeatExposureResult | None:
    """区间内相邻事件的超温积分（℃·秒），基于 clock-audit 同款校正时间。

    - 相邻事件（按 seq 升序，缺号不补点）间温度视为线性变化，对超过
      upper_limit 的部分做精确积分
    - 事件时间先按等权保序回归校正；校正后间隔为零或超过 max_gap_seconds
      的段不计入积分（也不出现在明细中）
    - 只在查询时即时计算，不改动任何原始记录；补报后重查即重算
    - 箱号不存在返回 None，由路由层转 404；区间无事件返回零值及空明细
    """
    events = _events_in_range(db, box_code, seq_from, seq_to)
    if events is None:
        return None

    corrected, _ = _isotonic_suggestions(events)

    segments: list[schemas.HeatExposureSegment] = []
    exposures: list[float] = []
    skipped = 0
    for i in range(len(events) - 1):
        gap_micros = corrected[i + 1] - corrected[i]
        gap_seconds = gap_micros / 1_000_000
        # 校正时间非递减，间隔只会 >= 0；为 0（同块）或超上限的段不计入
        if gap_micros <= 0 or gap_seconds > max_gap_seconds:
            skipped += 1
            continue
        exposure = _segment_exposure(
            events[i].temperature, events[i + 1].temperature,
            upper_limit, gap_seconds,
        )
        exposures.append(exposure)
        segments.append(
            schemas.HeatExposureSegment(
                start_seq=events[i].seq,
                end_seq=events[i + 1].seq,
                start_time=_from_epoch_micros(corrected[i]),
                end_time=_from_epoch_micros(corrected[i + 1]),
                gap_seconds=gap_seconds,
                start_temperature=events[i].temperature,
                end_temperature=events[i + 1].temperature,
                celsius_seconds=exposure,
            )
        )

    return schemas.HeatExposureResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        upper_limit=upper_limit,
        max_gap_seconds=max_gap_seconds,
        event_count=len(events),
        segment_count=len(segments),
        skipped_segment_count=skipped,
        total_celsius_seconds=math.fsum(exposures),
        segments=segments,
    )
