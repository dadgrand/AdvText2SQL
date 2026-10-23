import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ambrosia_subset import EXCLUDED_DATABASES, prepare_subset

ROOT = Path(__file__).resolve().parents[1]


class SubsetTests(unittest.TestCase):
    def test_subset_matches_historical_predictions_and_preserves_questions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            source = (ROOT / "data/ambrosia_large.json").read_bytes()
            (root / "data/ambrosia_large.json").write_bytes(source)
            target, manifest = prepare_subset(root)
            included = json.loads(target.read_text(encoding="utf-8"))
            historical = json.loads((ROOT / "research_results/ablation_results_large/feat_baseline/ambrosia_query_results.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["question_ids"]), set(historical))
            self.assertEqual(len(included), 227)
            self.assertEqual(len(manifest["excluded"]), 13)
            self.assertEqual(manifest["included_databases"], 52)
            self.assertEqual(manifest["dataset_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(included, [item for item in json.loads(source) if item["db_id"] not in EXCLUDED_DATABASES])
            timestamp = target.stat().st_mtime_ns
            prepare_subset(root)
            self.assertEqual(timestamp, target.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
