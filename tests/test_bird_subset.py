import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bird_subset import compare_subset, select_ambiguous
from feature_presets import feature_flags

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research_results/runs/gpt54mini_hybrid_large_20261016/bird"


class BirdSubsetTests(unittest.TestCase):
    def test_exact_selection_and_two_feature_difference(self):
        rows, manifest = select_ambiguous(ROOT, SOURCE)
        original = json.loads((ROOT / SOURCE / "query_results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 50)
        self.assertEqual({str(r["question_id"]) for r in rows},
                         {qid for qid, sql in original.items() if sql == "ambiguous"})
        old, new = feature_flags("hybrid"), feature_flags("hybrid_no32_33")
        self.assertEqual({k for k in old if old[k] != new[k]}, {"FEAT_32", "FEAT_33"})
        self.assertEqual(len(manifest["question_ids"]), 50)

    def test_composite_is_separate_from_subset_score(self):
        rows, manifest = select_ambiguous(ROOT, SOURCE)
        results = [{"question_id": str(row["question_id"]), "score": index < 10}
                   for index, row in enumerate(rows)]
        report = compare_subset(ROOT, None, manifest, {"results": results})
        self.assertEqual(report["subset_correct"], 10)
        self.assertEqual(report["source_full_correct"], 93)
        self.assertEqual(report["composite_full_correct"], 103)
        self.assertEqual(report["baseline_subset_correct"], 21)
        with self.assertRaises(ValueError):
            compare_subset(ROOT, None, manifest, {"results": results[:-1]})


if __name__ == "__main__":
    unittest.main()
