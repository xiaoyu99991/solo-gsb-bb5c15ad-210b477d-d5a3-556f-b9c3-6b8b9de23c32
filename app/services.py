"""业务逻辑：事件入库、幂等去重、乱序后的最新状态查询、时钟回拨校正、超温积分、轨迹压缩预览。"""
import math
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 单次轨迹压缩预览允许读取的最大区间事件数，超过则拒绝（422）
TRAJECTORY_COMPRESSION_MAX_EVENTS = 120


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
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    limit: int | None = None,
) -> list[models.BoxEvent] | None:
    """按 seq 升序取区间内事件（含端点）。

    箱号不存在（从未上报过）返回 None，由调用方转 404；
    箱号存在但区间内无事件返回 []。缺号不补点，只返回库中实际事件。
    limit 给定时最多返回该数量条（用于压缩预览的 120 条上限校验）。
    """
    box_exists = db.scalar(
        select(func.count())
        .select_from(models.BoxEvent)
        .where(models.BoxEvent.box_code == box_code)
    )
    if not box_exists:
        return None

    stmt = (
        select(models.BoxEvent)
        .where(
            models.BoxEvent.box_code == box_code,
            models.BoxEvent.seq >= seq_from,
            models.BoxEvent.seq <= seq_to,
        )
        .order_by(models.BoxEvent.seq)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


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


def _compress_trajectory(
    xs: list[int], ys: list[float], max_error: float
) -> tuple[list[int], Fraction]:
    """最少保留点温度轨迹压缩（DP 精确求解）。

    以序号为横坐标、温度为纵坐标，相邻保留点之间按温度线性插值重建被省略点。
    可行子序列要求：首末点必保留；每个被省略点的重建温度与原值之差 <= max_error。
    在全部可行子序列中选保留数最少的；保留数相同时取保留序号序列字典序最小者。

    方法：在“段 (i,j) 可行”构成的 DAG 上做最短路径 DP，段可行只取决于两端点，
    贪心“尽量跳最远”不能保证同数量下字典序最小，故用 DP 精确求解。
    误差用 Fraction 精确计算（float 可无损转为有理数，序号为整数），
    避免浮点误差在 max_error 边界上误判；区间事件数 <= 120，O(n^3) 足够快。

    返回 (保留点在输入中的下标序列, 被省略点的最大重建误差)。
    """
    n = len(xs)
    yf = [Fraction(y) for y in ys]
    tol = Fraction(max_error)

    def deviation(i: int, k: int, j: int) -> Fraction:
        """点 k 相对弦 (i,j) 线性插值的重建误差绝对值（精确有理数）。"""
        interp = yf[i] + (yf[j] - yf[i]) * Fraction(xs[k] - xs[i], xs[j] - xs[i])
        return abs(yf[k] - interp)

    if n <= 2:  # 0/1/2 条：首末保留即全部，没有被省略点
        return list(range(n)), Fraction(0)

    def feasible(i: int, j: int) -> bool:
        return all(deviation(i, k, j) <= tol for k in range(i + 1, j))

    # best[i] = (从第 i 个事件到末尾的最少保留数, 对应的最优保留下标序列)
    best: list[tuple[int, tuple[int, ...]]] = [(0, ())] * n
    best[n - 1] = (1, (n - 1,))
    for i in range(n - 2, -1, -1):
        chosen: tuple[int, tuple[int, ...]] | None = None
        for j in range(i + 1, n):
            if not feasible(i, j):
                continue
            # 元组比较：先比保留数（最少），数量相同再比下标序列（字典序最小）；
            # 所有候选序列同以 i 开头，等价于挑后缀最优的可行后继 j
            cand = (best[j][0] + 1, (i,) + best[j][1])
            if chosen is None or cand < chosen:
                chosen = cand
        assert chosen is not None  # 相邻两点构成的段没有中间点，必然可行
        best[i] = chosen
    kept = list(best[0][1])

    actual_max = Fraction(0)
    for a, b in zip(kept, kept[1:]):
        for k in range(a + 1, b):
            dev = deviation(a, k, b)
            if dev > actual_max:
                actual_max = dev
    return kept, actual_max


def trajectory_compression(
    db: Session, box_code: str, seq_from: int, seq_to: int, max_error: float
) -> schemas.TrajectoryCompressionResult | None:
    """单箱温度轨迹压缩预览（只读即时计算，不写回任何原始记录）。

    - 取区间 [seq_from, seq_to]（含端点）内按 seq 升序的事件，缺号不补点；
      区间事件超过 TRAJECTORY_COMPRESSION_MAX_EVENTS（120）条 -> 422
    - 以序号为横坐标做最少保留点压缩，保留点之间线性插值重建被省略事件，
      重建误差不得超过 max_error；同数量选保留序号字典序最小者
    - 箱号不存在（从未上报过）返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回空结果；仅一条事件：直接保留
    """
    events = _events_in_range(
        db,
        box_code,
        seq_from,
        seq_to,
        limit=TRAJECTORY_COMPRESSION_MAX_EVENTS + 1,
    )
    if events is None:
        return None
    if len(events) > TRAJECTORY_COMPRESSION_MAX_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "too_many_events",
                "message": (
                    f"序号区间 [{seq_from}, {seq_to}] 内事件超过 "
                    f"{TRAJECTORY_COMPRESSION_MAX_EVENTS} 条，请缩小区间后重试"
                ),
                "seq_from": seq_from,
                "seq_to": seq_to,
                "limit": TRAJECTORY_COMPRESSION_MAX_EVENTS,
            },
        )

    if not events:
        return schemas.TrajectoryCompressionResult(
            box_code=box_code,
            seq_from=seq_from,
            seq_to=seq_to,
            max_error=max_error,
            event_count=0,
            kept_count=0,
            omitted_count=0,
            actual_max_error=None,
            kept_events=[],
        )

    xs = [e.seq for e in events]
    ys = [e.temperature for e in events]
    kept_idx, actual_max = _compress_trajectory(xs, ys, max_error)

    return schemas.TrajectoryCompressionResult(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        max_error=max_error,
        event_count=len(events),
        kept_count=len(kept_idx),
        omitted_count=len(events) - len(kept_idx),
        actual_max_error=float(actual_max),
        kept_events=[
            schemas.TrajectoryCompressionPoint(
                seq=events[i].seq,
                event_time=events[i].event_time,
                temperature=events[i].temperature,
            )
            for i in kept_idx
        ],
    )
