#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import unittest
from datetime import datetime

from limit_pool_client import fetch_pools, parse_pools
from limit_up_policy import LimitCandidatePolicy


def payload(rows):
    return "callback(" + json.dumps({"rc": 0, "data": {"pool": rows}}) + ")"


class LimitPoolClientTests(unittest.TestCase):
    def test_parse_eastmoney_up_pool_normalizes_seal_metrics(self):
        up = payload([{
            "c": "600000", "n": "浦发银行", "p": 10000, "zdp": 10.0,
            "fund": 50000000, "amount": 200000000, "ltsz": 2000000000,
            "zbc": 0, "fbt": 93500, "lbt": 140000, "lbc": 2, "hybk": "银行",
        }])
        result = parse_pools(up, payload([]))
        row = result["up"][0]
        self.assertEqual(row["code"], "sh600000")
        self.assertEqual(row["source"], "EASTMONEY_LIMIT_POOL")
        self.assertEqual(row["seal_to_turnover_pct"], 25.0)
        self.assertEqual(row["seal_to_float_pct"], 2.5)
        self.assertEqual(row["open_count"], 0)
        self.assertTrue(LimitCandidatePolicy.eligible(row))

    def test_down_pool_uses_down_open_count_and_change_sort(self):
        down = payload([
            {"c": "000001", "n": "甲", "p": 9000, "zdp": -8.0, "oc": 1},
            {"c": "000002", "n": "乙", "p": 8000, "zdp": -10.0, "oc": 2},
        ])
        result = parse_pools(payload([]), down)
        self.assertEqual([row["code"] for row in result["down"]], ["sz000002", "sz000001"])
        self.assertEqual(result["down"][0]["open_count"], 2)

    def test_fetch_uses_injected_client_and_query_date(self):
        calls = []

        def fetcher(url, **kwargs):
            calls.append(url)
            return payload([])

        result = fetch_pools(datetime(2026, 10, 23), fetcher=fetcher)
        self.assertEqual(len(calls), 2)
        self.assertIn("date=20261023", calls[0])
        self.assertEqual(result["source"], "EASTMONEY_LIMIT_POOL")


if __name__ == "__main__":
    unittest.main()
