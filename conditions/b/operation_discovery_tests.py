import unittest
from conditions.b.operation_discovery import metric_phrases


class OperationDiscoveryTests(unittest.TestCase):
    def test_extracts_multiword_named_metric(self):
        phrases = metric_phrases("Compare median long branch scores between fungi and animals")
        self.assertIn("long branch score", phrases)

    def test_does_not_trigger_without_metric_marker(self):
        self.assertEqual(metric_phrases("How many variants are present?"), [])


if __name__ == "__main__":
    unittest.main()
