import unittest

from src.he_rong_lian_strategy import Features, decide


BASE = dict(
    current_position=0.5,
    monthly_trend_up=True,
    weekly_trend_up=True,
    annual_ma_up=True,
    daily_above_ma20=True,
    daily_above_ma60=True,
    monthly_macd_positive=True,
)


class StrategyDecisionTests(unittest.TestCase):
    def test_clean_bull_trend_holds(self):
        d = decide(Features(**BASE))
        self.assertEqual(d.regime, "TREND_UP")
        self.assertEqual(d.target_position, 0.75)

    def test_three_new_highs_with_divergence_exits(self):
        d = decide(Features(**BASE, price_new_high=True, new_high_count_20d=3,
                            daily_divergence=True, weekly_divergence=True))
        self.assertEqual(d.regime, "TOP_RISK")
        self.assertLessEqual(d.target_position, 0.15)

    def test_long_bull_extreme_drawdown_buys(self):
        d = decide(Features(**(BASE | dict(daily_above_ma20=False, monthly_return=-0.22,
                            support_breached=True, shock_drop_atr=-2.5,
                            after_high_volume_drop_volume_ratio=0.6))))
        self.assertEqual(d.regime, "PANIC_BUY")
        self.assertEqual(d.target_position, 0.80)

    def test_second_selloff_blocks_bottom_fishing(self):
        d = decide(Features(**(BASE | dict(daily_above_ma20=False, monthly_return=-0.10,
                            support_breached=True, selloff_number=2,
                            daily_divergence=True, weekly_divergence=True,
                            weekly_macd_weakening=True))))
        self.assertEqual(d.regime, "SECOND_SELLOFF_RISK")

    def test_extreme_monthly_drop_overrides_second_selloff_rule(self):
        d = decide(Features(**(BASE | dict(daily_above_ma20=False, monthly_return=-0.22,
                            support_breached=True, selloff_number=2,
                            daily_divergence=True, weekly_divergence=True,
                            weekly_macd_weakening=True))))
        self.assertEqual(d.regime, "PANIC_BUY")

    def test_first_rebound_volume_at_resistance_sells(self):
        d = decide(Features(**(BASE | dict(daily_above_ma20=False, first_stage_rebound=True,
                            near_resistance=True, volume_ratio_20d=1.3))))
        self.assertEqual(d.regime, "REBOUND_DISTRIBUTION")

    def test_missing_data_does_not_trade(self):
        d = decide(Features(**BASE, data_ready=False))
        self.assertEqual(d.action, "不交易")
        self.assertEqual(d.target_position, 0.5)


if __name__ == "__main__":
    unittest.main()
