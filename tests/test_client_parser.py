"""双版式解析与损坏条目跳过测试。"""
import unittest

from birdsurvey.client import parse_items


class TestParseItems(unittest.TestCase):
    def test_table_layout_valid_and_corrupt(self):
        items = [
            {"id": "a1", "ts": 100.0, "species": "HERON", "freq_hz": 2000},
            {"ts": 101.0, "freq_hz": 2100},  # 损坏：缺 id/species
            {"id": "a2", "ts": 102.0, "species": "TERN", "confidence": 0.9},
        ]
        records, bad = parse_items(items, "table")
        self.assertEqual(bad, 1)
        self.assertEqual([r["id"] for r in records], ["a1", "a2"])

    def test_card_layout_field_mapping(self):
        items = [
            {"card_id": "b1", "when": 50.5, "bird": "RAIL", "freq_hz": 3000},
            {"damaged": True, "raw": "???"},  # 损坏卡片
            {"card_id": "", "when": 51.0, "bird": "RAIL"},  # 空 id
            {"card_id": "b2", "when": "bad", "bird": "RAIL"},  # 非法时间
        ]
        records, bad = parse_items(items, "card")
        self.assertEqual(bad, 3)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "b1")
        self.assertEqual(records[0]["species"], "RAIL")
        self.assertEqual(records[0]["ts"], 50.5)

    def test_unknown_layout_rejected(self):
        with self.assertRaises(ValueError):
            parse_items([{"id": "x"}], "grid")


if __name__ == "__main__":
    unittest.main()
