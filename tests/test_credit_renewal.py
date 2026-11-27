import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

CONSOLE = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/flow/console'
spec = importlib.util.spec_from_file_location('credit_reader', CONSOLE / 'balance_reader.py')
reader = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(CONSOLE), *sys.path]):
    spec.loader.exec_module(reader)


class CreditRenewalTest(unittest.TestCase):
    def test_official_tooltip(self):
        self.assertEqual(reader.parse_credit_renewal('1,050 个 Google Flow 点数\n重置时间：11月19日20:30',datetime(2026,11,10)),'2026-11-19 20:30')
    def test_year_boundary(self):
        self.assertEqual(reader.parse_credit_renewal('Google Flow 点数\n重置时间：1月2日20:30',datetime(2026,12,29)),'2027-01-02 20:30')
    def test_observation_is_not_renewal(self):
        self.assertIsNone(reader.parse_credit_renewal('积分余额 125\n更新时间：11月19日20:30'))
    def test_invalid_date(self):
        self.assertIsNone(reader.parse_credit_renewal('Google Flow 点数\n重置时间：2月30日20:30'))

    def test_only_the_remaining_daily_balance_matches(self):
        for line, expected in [('今天还剩 29 个 Flow 点数。', 29), ('今天还剩 0 个 Flow 点数。', 0),
                               ('You have 8 Flow credits left today.', 8),
                               ('您每天还会额外获得 50 个 Google Flow 点数。', None),
                               ('1,029 个 Google Flow 点数', None)]:
            with self.subTest(line=line):
                match = reader.DAILY_CREDITS_TEXT.fullmatch(line)
                self.assertEqual(int(match[1]) if match else None, expected)
