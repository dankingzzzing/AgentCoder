"""Three trusted teaching tasks; deterministic agents are explicitly not LLMs."""

from dataclasses import dataclass

from . import demo
from .errors import AgentError


@dataclass(frozen=True)
class TeachingCase:
    id: str
    title: str
    task: str
    buggy: str
    fixed: str
    tests: str
    defects: tuple


MONEY_BUGGY = '''def total_price(prices, discount=0):
    """Return a two-decimal string. Reject bool, negative/nonfinite prices and invalid discounts."""
    total = sum(prices)
    return str(round(total * discount, 2))
'''

MONEY_FIXED = '''from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def total_price(prices, discount=0):
    """Return a two-decimal string. Reject bool, negative/nonfinite prices and invalid discounts."""
    def number(value):
        if isinstance(value, bool):
            raise ValueError("Boolean is not a price")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Expected a finite number") from None
        if not result.is_finite():
            raise ValueError("Expected a finite number")
        return result
    rate = number(discount)
    if not 0 <= rate <= 1:
        raise ValueError("Discount must be between 0 and 1")
    values = [number(price) for price in prices]
    if any(value < 0 for value in values):
        raise ValueError("Negative price")
    total = sum(values, Decimal(0)) * (1 - rate)
    return format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
'''

MONEY_TESTS = '''import unittest
from solution import total_price

class PriceTests(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(total_price([10, 20]), "30.00")
    def test_discount(self):
        self.assertEqual(total_price([10, 20], .25), "22.50")
    def test_half_up(self):
        self.assertEqual(total_price(["2.675"]), "2.68")
    def test_empty(self):
        self.assertEqual(total_price([]), "0.00")
    def test_full_discount(self):
        self.assertEqual(total_price([50], 1), "0.00")
    def test_invalid_prices(self):
        for price in (True, -1, "NaN", "Infinity", "bad"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                total_price([price])
    def test_invalid_discounts(self):
        for rate in (True, -.1, 1.1, "NaN", "bad"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                total_price([10], rate)
    def test_decimal_sum(self):
        self.assertEqual(total_price(["0.1", "0.2"]), "0.30")
'''

TEXT_BUGGY = '''def word_counts(text):
    """Count Unicode words case-insensitively, excluding punctuation and underscores."""
    counts = {}
    for word in text.split():
        counts[word] = counts.get(word, 0) + 1
    return counts

def top_words(text, n=3):
    """Return (word, count) pairs by descending count then ascending word; n is a nonnegative int."""
    return sorted(word_counts(text).items(), reverse=True)[:n]
'''

TEXT_FIXED = '''import re
from collections import Counter

def word_counts(text):
    """Count Unicode words case-insensitively, excluding punctuation and underscores."""
    if not isinstance(text, str):
        raise TypeError("Expected text")
    return dict(Counter(re.findall(r"[^\\W_]+", text.casefold())))

def top_words(text, n=3):
    """Return (word, count) pairs by descending count then ascending word; n is a nonnegative int."""
    if type(n) is not int or n < 0:
        raise ValueError("Expected a nonnegative integer")
    return sorted(word_counts(text).items(), key=lambda item: (-item[1], item[0]))[:n]
'''

TEXT_TESTS = '''import unittest
from solution import word_counts, top_words

class WordTests(unittest.TestCase):
    def test_case(self):
        self.assertEqual(word_counts("Hello HELLO hello"), {"hello": 3})
    def test_punctuation(self):
        self.assertEqual(word_counts("cat, cat! dog_cat"), {"cat": 3, "dog": 1})
    def test_unicode(self):
        self.assertEqual(word_counts("Straße STRASSE 中文"), {"strasse": 2, "中文": 1})
    def test_empty(self):
        self.assertEqual(word_counts("..."), {})
    def test_order(self):
        self.assertEqual(top_words("b a b c a c c"), [("c", 3), ("a", 2), ("b", 2)])
    def test_limits(self):
        self.assertEqual(top_words("a b", 0), [])
        self.assertEqual(top_words("a", 10), [("a", 1)])
    def test_invalid_n(self):
        for n in (-1, True, 1.5, "2"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                top_words("a", n)
    def test_invalid_text(self):
        with self.assertRaises(TypeError):
            word_counts(42)
'''

CASES = {
    "contracts": TeachingCase("contracts", "输入契约与异常处理", demo.TASK, demo.BUGGY, demo.FIXED, demo.TESTS, (
        ("    return sum(values) / len(values)", "空序列导致除零", "为空序列返回 0.0。", "medium"),
        ("def add_tag(tag, tags=[]):", "默认列表被多次调用共享", "默认值改为 None，每次创建新列表。", "medium"),
        ("    return eval(raw)", "用户字符串被 eval 执行", "使用 json.loads，拒绝布尔值与非数值类型。", "high"),
        ("    except:", "异常被吞掉", "让 ValueError 向调用者传播。", "medium"),
    )),
    "prices": TeachingCase("prices", "订单金额与精度", "实现 total_price(prices, discount=0)：接受数值或数值字符串列表；discount 为 0 到 1 的折扣比例；返回两位小数字符串，按 ROUND_HALF_UP 舍入。拒绝布尔值、负价、非有限值与非法折扣，非法值抛 ValueError；空订单为 0.00。", MONEY_BUGGY, MONEY_FIXED, MONEY_TESTS, (
        ("    total = sum(prices)", "金额类型和范围未校验", "使用 Decimal 转换，拒绝布尔值、负值和非有限值。", "medium"),
        ("    return str(round(total * discount, 2))", "折扣方向和舍入契约错误", "乘以 1-discount，校验折扣并固定两位 HALF_UP 舍入。", "medium"),
    )),
    "words": TeachingCase("words", "文本统计与稳定排序", "实现 word_counts(text) 和 top_words(text, n=3)：按 Unicode 单词统计，使用 casefold 忽略大小写，标点和下划线为分隔符；top_words 返回 (word,count) 列表，次数降序、同频单词升序。n 必须为非负整数且不接受 bool，非法 n 抛 ValueError；非字符串 text 抛 TypeError。", TEXT_BUGGY, TEXT_FIXED, TEXT_TESTS, (
        ("    for word in text.split():", "大小写与分词不符合契约", "校验字符串，casefold 后按 Unicode 单词分词。", "medium"),
        ("    return sorted(word_counts(text).items(), reverse=True)[:n]", "排序与参数校验错误", "校验 n，按次数降序、单词升序排序。", "medium"),
    )),
}


def get_case(case_id="contracts"):
    if not isinstance(case_id, str) or case_id not in CASES:
        raise AgentError("教学任务不存在。")
    return CASES[case_id]


def catalog():
    return [{"id": case.id, "title": case.title, "task": case.task} for case in CASES.values()]
