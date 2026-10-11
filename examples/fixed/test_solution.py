import unittest
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
