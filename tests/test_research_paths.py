from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research_paths import configurations_match, resolve_artifact_path

ROOT = Path(__file__).resolve().parents[1]


class ResearchPathTests(unittest.TestCase):
    def test_old_and_new_artifact_paths_resolve_to_same_location(self):
        expected = ROOT / "research_results/runs/example/bird"
        self.assertEqual(resolve_artifact_path(ROOT, "runs/example/bird"), expected)
        self.assertEqual(resolve_artifact_path(ROOT, ROOT / "runs/example/bird"), expected)
        self.assertEqual(resolve_artifact_path(ROOT, expected), expected)
        self.assertEqual(resolve_artifact_path(ROOT, "ablation_results_large/feat_baseline"),
                         ROOT / "research_results/ablation_results_large/feat_baseline")

    def test_relocation_preserves_checkpoint_identity_but_not_changed_configuration(self):
        old = {"dataset_sha256": "same", "diagnostic_selection": {
            "source_run": str(ROOT / "runs/example/bird"), "source_checkpoint_sha256": "source"}}
        relocated = {"dataset_sha256": "same", "diagnostic_selection": {
            "source_run": str(ROOT / "research_results/runs/example/bird"), "source_checkpoint_sha256": "source"}}
        self.assertTrue(configurations_match(ROOT, old, relocated))
        relocated["diagnostic_selection"]["source_checkpoint_sha256"] = "different"
        self.assertFalse(configurations_match(ROOT, old, relocated))

    def test_source_manifests_from_completed_diagnostic_remain_readable(self):
        from bird_subset import compare_subset
        import json
        folder = ROOT / "research_results/runs/gpt54mini_hybrid_bird_no32_33_50_20261023/bird"
        manifest = json.loads((folder / "selection_manifest.json").read_text(encoding="utf-8"))
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        comparison = compare_subset(ROOT, folder, manifest, report)
        self.assertEqual(comparison["subset_correct"], 22)
        self.assertEqual(comparison["composite_full_correct"], 115)


if __name__ == "__main__":
    unittest.main()
