import unittest

from server import _parse_stock_valuation_rows


class StockScoreTests(unittest.TestCase):
    def test_bank_valuation_does_not_require_ev_ebitda(self):
        rows = [
            {"CORRE_SECURITY_CODE": "行业中值", "CORRE_SECURITY_NAME": "行业中值", "PE_TTM": 5.97, "PB_MRQ": 0.54},
            {"CORRE_SECURITY_CODE": "600036", "CORRE_SECURITY_NAME": "招商银行", "PE_TTM": 6.51, "PB_MRQ": 0.87},
        ]

        result = _parse_stock_valuation_rows("600036", rows)

        self.assertEqual(result["市盈率-TTM"], {"value": 6.51, "peerMedian": 5.97})
        self.assertEqual(result["市净率-MRQ"], {"value": 0.87, "peerMedian": 0.54})
        self.assertNotIn("EV/EBITDA-24A", result)


if __name__ == "__main__":
    unittest.main()
