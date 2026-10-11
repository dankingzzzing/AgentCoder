"""Trusted, repeatable fixtures. These agents do not call an LLM."""

TASK = "实现 solution.py：average(values) 计算平均值，空序列返回 0.0；add_tag(tag, tags=None) 的独立调用不能共享列表；parse_score(raw) 只接受 JSON 数值，拒绝表达式、布尔值和其他类型；load_score(raw) 转换整数字符串，无效输入抛出 ValueError。"

BUGGY = '''def average(values):
    """Return the mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values)

def add_tag(tag, tags=[]):
    """Independent calls should have independent lists."""
    tags.append(tag)
    return tags

def parse_score(raw):
    """Accept only numeric JSON values."""
    return eval(raw)

def load_score(raw):
    """Invalid integer strings must raise ValueError."""
    try:
        return int(raw)
    except:
        pass
'''

FIXED = '''import json

def average(values):
    """Return the mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0

def add_tag(tag, tags=None):
    """Independent calls should have independent lists."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags

def parse_score(raw):
    """Accept only numeric JSON values."""
    value = json.loads(raw)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a numeric JSON value")
    return value

def load_score(raw):
    """Invalid integer strings must raise ValueError."""
    return int(raw)
'''

TESTS = '''import unittest
from solution import average, add_tag, parse_score, load_score

class ContractTests(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(average([2,4,6]), 4.0)
    def test_empty(self):
        self.assertEqual(average([]), 0.0)
    def test_independent_lists(self):
        first, second = add_tag("first"), add_tag("second")
        self.assertEqual(first, ["first"])
        self.assertEqual(second, ["second"])
        self.assertIsNot(first, second)
    def test_numbers(self):
        self.assertEqual(parse_score("12.5"), 12.5)
    def test_expression(self):
        with self.assertRaises(ValueError):
            parse_score("1 + 2")
    def test_non_numbers(self):
        for raw in ('true', 'null', '"x"', '[]', '{}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_score(raw)
    def test_integer(self):
        self.assertEqual(load_score("42"), 42)
    def test_invalid_integer(self):
        with self.assertRaises(ValueError):
            load_score("invalid")

if __name__ == "__main__":
    unittest.main()
'''
