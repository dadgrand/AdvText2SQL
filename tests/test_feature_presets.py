from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from feature_presets import feature_flags

ROOT = Path(__file__).resolve().parents[1]


class FeaturePresetTests(unittest.TestCase):
    def test_hybrid_matches_saved_best_configuration(self):
        summary = (ROOT / "research_results/ablation_results_small/feat_best_hybrid_large/summary.txt").read_text(encoding="utf-8")
        expected = set(re.findall(r"FEAT_\d+", summary))
        actual = {name for name, enabled in feature_flags("hybrid").items() if enabled}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 14)

    def test_baseline_remains_all_disabled(self):
        flags = feature_flags("baseline")
        self.assertEqual(len(flags), 36)
        self.assertFalse(any(flags.values()))


if __name__ == "__main__":
    unittest.main()
