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


if __name__ == "__main__":
    unittest.main()
