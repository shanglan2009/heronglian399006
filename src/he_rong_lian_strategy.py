"""和融联择机策略的无依赖参考决策引擎。

输入应是由行情特征层生成的日终特征；本模块只负责市场状态与仓位目标，
不负责数据下载、复权、成交成本、下单或回测。
"""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Features:
    current_position: float
    monthly_trend_up: bool
    weekly_trend_up: bool
    annual_ma_up: bool
    daily_above_ma20: bool
    daily_above_ma60: bool
    monthly_macd_positive: bool
    weekly_macd_weakening: bool = False
    daily_divergence: bool = False
    weekly_divergence: bool = False
    price_new_high: bool = False
    new_high_count_20d: int = 0
    single_day_turnover: float = 0.0
    rolling_20d_turnover: float = 0.0
    weekly_ma_deviation: float = 0.0
    monthly_return: float = 0.0
    shock_drop_atr: float = 0.0
    volume_ratio_20d: float = 1.0
    after_high_volume_drop_volume_ratio: float = 1.0
    near_support: bool = False
    support_breached: bool = False
    near_resistance: bool = False
    first_stage_rebound: bool = False
    selloff_number: int = 0
    ma_cluster_high: bool = False
    high_level_reversal_pattern: bool = False
    data_ready: bool = True


@dataclass(frozen=True)
class Decision:
    regime: str
    target_position: float
    action: str
    confidence: str
    reasons: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)


DEFAULT = {
    "single_day_turnover_hot": 0.07,
    "rolling_20d_turnover_hot": 1.0,
    "weekly_ma_deviation_hot": 0.15,
    "monthly_panic_return": -0.20,
    "rapid_drop_atr": -2.0,
    "volume_contraction_ratio": 0.75,
    "rebound_distribution_volume_ratio": 1.20,
    "new_high_cluster_count": 3,
    "risk_score_defensive": 3,
    "risk_score_exit": 5,
    "defensive": 0.15,
    "range_low": 0.35,
    "range_high": 0.60,
    "trend_hold": 0.75,
    "panic_buy": 0.80,
    "maximum": 0.90,
}


def _long_trend_score(f: Features) -> int:
    return sum((f.monthly_trend_up, f.weekly_trend_up, f.annual_ma_up, f.monthly_macd_positive))


def _risk_score(f: Features, p: dict) -> int:
    return sum(
        (
            f.daily_divergence,
            f.weekly_divergence,
            f.weekly_macd_weakening,
            f.ma_cluster_high,
            f.high_level_reversal_pattern,
            f.single_day_turnover >= p["single_day_turnover_hot"],
            f.rolling_20d_turnover >= p["rolling_20d_turnover_hot"],
            f.weekly_ma_deviation >= p["weekly_ma_deviation_hot"],
            f.new_high_count_20d >= p["new_high_cluster_count"],
        )
    )


def decide(f: Features, params: dict | None = None) -> Decision:
    """按最高优先级规则返回唯一仓位目标；所有仓位均为总资产比例。"""
    p = DEFAULT | (params or {})
    if not f.data_ready:
        return Decision("DATA_BLOCKED", f.current_position, "不交易", "低", ["关键特征缺失"], [])

    long_score = _long_trend_score(f)
    risk = _risk_score(f, p)
    extreme_monthly = f.monthly_return <= p["monthly_panic_return"]
    panic = extreme_monthly or f.shock_drop_atr <= p["rapid_drop_atr"]
    contracted = f.after_high_volume_drop_volume_ratio <= p["volume_contraction_ratio"]

    # 最新且最明确的顶部约束优先：背离、三次新高或二次急杀时先保命。
    clustered_high = f.price_new_high and f.new_high_count_20d >= p["new_high_cluster_count"]
    if risk >= p["risk_score_exit"] or (clustered_high and (f.daily_divergence or f.weekly_divergence)):
        return Decision(
            "TOP_RISK",
            min(f.current_position, p["defensive"]),
            "减仓至防守仓位",
            "高",
            [f"风险计数={risk}", "新高/背离/过热信号共振"],
            ["A06", "A07", "A08", "A37", "A38", "A39"],
        )

    # 2026-07-17与07-24把“月跌幅约-20%+长周期仍强”升级为最高级超跌买点，
    # 因而覆盖2026-06-18对第二次急杀不抄底的一般限制。
    if long_score >= 3 and panic and (f.near_support or f.support_breached) and (
        extreme_monthly or contracted or f.selloff_number <= 1
    ):
        return Decision(
            "PANIC_BUY",
            min(p["panic_buy"], p["maximum"]),
            "分三批低吸",
            "中",
            [f"长趋势得分={long_score}/4", "月跌幅或ATR达到极端", "结构支撑有效或被短暂跌破"],
            ["A16", "A26", "A29", "A40", "A41"],
        )

    if f.selloff_number >= 2 and risk >= p["risk_score_defensive"]:
        return Decision(
            "SECOND_SELLOFF_RISK",
            min(f.current_position, p["range_low"]),
            "禁止二次抄底并降低仓位",
            "高",
            ["高位风险尚未解除", "这是同一顶部结构后的第二次急杀", "尚未达到后续文章定义的极端超跌条件"],
            ["A37", "A38", "A40", "A41"],
        )

    # 2026-08-14的最新反弹规则覆盖此前“反弹继续持有”的一般表述。
    if f.first_stage_rebound and f.near_resistance and f.volume_ratio_20d >= p["rebound_distribution_volume_ratio"]:
        return Decision(
            "REBOUND_DISTRIBUTION",
            min(f.current_position, p["range_low"]),
            "反弹放量减仓",
            "高",
            ["下跌后第一阶段反弹", "阻力附近放量"],
            ["A43"],
        )

    if long_score >= 3 and f.daily_above_ma20 and risk < p["risk_score_defensive"]:
        return Decision(
            "TREND_UP",
            p["trend_hold"],
            "持有；仅在回测支撑时加仓",
            "中",
            [f"长趋势得分={long_score}/4", "日线位于20日线上", f"风险计数={risk}"],
            ["A20", "A21", "A31", "A32", "A33"],
        )

    if long_score >= 2:
        target = p["range_high"] if f.near_support else p["range_low"] if f.near_resistance else 0.50
        return Decision(
            "RANGE",
            target,
            "支撑低吸、阻力高卖；禁止追涨杀跌",
            "中",
            [f"长趋势得分={long_score}/4", f"风险计数={risk}", "短周期与长周期不同步"],
            ["A10", "A17", "A18", "A19", "A34", "A35", "A45"],
        )

    return Decision(
        "DOWNTREND",
        p["defensive"],
        "保持防守，等待长期趋势或极端超跌条件成立",
        "中",
        [f"长趋势得分={long_score}/4", f"风险计数={risk}"],
        ["A01"],
    )
