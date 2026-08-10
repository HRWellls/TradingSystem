import unittest

from server import calculate_dca_backtest


def history(values):
    points = []
    for index, value in enumerate(values, 1):
        point = {"date": f"2026-08-{index:02d}", "nav": value[0]}
        if len(value) > 1:
            point["accumulatedNav"] = value[1]
        points.append(point)
    return {"points": points}


class DcaBacktestTests(unittest.TestCase):
    def test_period_filters_one_year_three_years_and_all_history(self):
        points = [
            {"date": "2020-08-10", "nav": 1.0},
            {"date": "2023-08-09", "nav": 1.1},
            {"date": "2023-08-10", "nav": 1.2},
            {"date": "2025-08-09", "nav": 1.3},
            {"date": "2025-08-10", "nav": 1.4},
            {"date": "2026-08-10", "nav": 1.5},
        ]

        one_year = calculate_dca_backtest("019547", {"points": points}, "1y")
        three_years = calculate_dca_backtest("019547", {"points": points}, "3y")
        all_history = calculate_dca_backtest("019547", {"points": points}, "all")

        self.assertEqual(one_year["period"]["start"], "2025-08-10")
        self.assertEqual(three_years["period"]["start"], "2023-08-10")
        self.assertEqual(all_history["period"]["start"], "2020-08-10")
        self.assertEqual(all_history["period"]["label"], "成立以来")

    def test_period_cutoff_handles_leap_day(self):
        points = [
            {"date": "2023-02-28", "nav": 1.0},
            {"date": "2024-02-29", "nav": 1.1},
        ]
        result = calculate_dca_backtest("019547", {"points": points}, "1y")
        self.assertEqual(result["period"]["start"], "2023-02-28")

    def test_rejects_unknown_period(self):
        with self.assertRaisesRegex(ValueError, "不支持的回测周期"):
            calculate_dca_backtest("019547", history([(1.0,), (1.1,)]), "5y")

    def test_drawdown_uses_rolling_period_high(self):
        result = calculate_dca_backtest(
            "019547",
            history([(100,), (120,), (108,), (108,)]),
        )

        self.assertEqual([point["multiple"] for point in result["series"]], [1, 1, 1, 2])
        self.assertAlmostEqual(result["series"][-1]["signalDrawdown"], -10.0)

    def test_new_high_resets_drawdown_basis(self):
        result = calculate_dca_backtest(
            "019547",
            history([(100,), (90,), (110,), (99,), (99,)]),
        )

        self.assertAlmostEqual(result["series"][-1]["signalDrawdown"], -10.0)
        self.assertEqual(result["series"][-1]["multiple"], 2)

    def test_accumulated_nav_prevents_distribution_false_drawdown(self):
        result = calculate_dca_backtest(
            "019547",
            history([(1.0, 1.0), (0.9, 1.0), (0.9, 1.0)]),
        )

        self.assertEqual([point["multiple"] for point in result["series"]], [1, 1, 1])

    def test_unit_nav_is_still_used_as_purchase_price(self):
        result = calculate_dca_backtest(
            "019547",
            history([(1.0, 1.0), (0.5, 1.0)]),
        )

        self.assertEqual(result["metrics"]["strategyInvested"], 40.0)
        self.assertEqual(result["metrics"]["strategyValue"], 30.0)

    def test_profit_amount_metrics_include_fixed_and_relative_values(self):
        result = calculate_dca_backtest("019547", history([(1.0,), (0.5,)]))
        metrics = result["metrics"]
        self.assertEqual(metrics["fixedProfit"], -10.0)
        self.assertEqual(metrics["excessProfit"], 0.0)
        self.assertEqual(metrics["excessProfit"], metrics["strategyProfit"] - metrics["fixedProfit"])


if __name__ == "__main__":
    unittest.main()
