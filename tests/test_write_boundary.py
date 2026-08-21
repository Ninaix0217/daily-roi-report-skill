from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_roi_lib import LocalMemory, RuntimePaths, norm, resolve_review_batch, run_report  # noqa: E402
from evidence_resolution import INFERRED_REVIEW, alias_family  # noqa: E402


STORE = "示例商店-A"
PRODUCT = "青木液"
SOURCES = {"青1", "青3"}
TARGET_DATE = "2026-08-18"


def synthetic_template() -> dict:
    return {
        "report": {
            "sheet": "日报",
            "template_date": TARGET_DATE,
            "date_cell": "A1",
            "total_cost_cell": "C8",
            "total_sales_cell": "D8",
            "total_roi_cell": "E8",
            "products": [{
                "name": PRODUCT,
                "row": 4,
                "cost_cell": "C4",
                "sales_cell": "D4",
                "roi_cell": "E4",
            }],
        },
        "sku": None,
        "store_groups": [{"store": STORE, "products": [PRODUCT]}],
    }


def synthetic_financial_book() -> dict:
    return {
        "sheets": [{
            "name": "财务",
            "values": [
                ["流水单号", "投放账户名称", "投放日期", "交易类型", "支出"],
                ["TX-SYNTHETIC-1", STORE, TARGET_DATE, "快车扣费", "123.45"],
            ],
        }],
    }


def store_assignment_mapping() -> dict[str, str]:
    families = [alias_family("青1")]
    token = "regular_store:" + hashlib.sha256(
        json.dumps(families, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {f"campaign:{norm(token)}": STORE}


class RunnerWriteBoundaryTests(unittest.TestCase):
    def _environment(self, root: Path):
        workspace = root / "workspace"
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "template.xlsx").write_bytes(b"synthetic-template-placeholder")
        (input_dir / "finance.xlsx").write_bytes(b"synthetic-financial-placeholder")
        (input_dir / "campaign-20260818.csv").write_text(
            "计划名称,花费,日期\n青1,60.00,2026-08-18\n青3,63.45,2026-08-18\n",
            encoding="utf-8-sig",
        )

        bridge = MagicMock(name="synthetic_workbook_bridge")
        bridge.inspect_template.side_effect = lambda path: (
            deepcopy(synthetic_template()) if Path(path).name == "template.xlsx" else None
        )
        bridge.inspect_xlsx.return_value = synthetic_financial_book()

        def fake_write(_template: Path, _payload: Path, output: Path) -> dict:
            Path(output).write_bytes(b"synthetic-written-workbook")
            return {"status": "PASS"}

        bridge.write.side_effect = fake_write
        bridge.verify.return_value = {"status": "PASS", "failures": []}
        dependencies = {"dependency_check": "PASS", "node": "node", "node_modules": "node_modules"}
        layout = {"sheets": [{"name": "日报", "merges": [], "cols": [], "row_heights": [], "cell_styles": []}], "styles_sha256": None}
        patches = (
            patch("daily_roi_lib.dependency_preflight", return_value=dependencies),
            patch("daily_roi_lib.WorkbookBridge", return_value=bridge),
            patch("daily_roi_lib.xlsx_layout_snapshot", return_value=layout),
        )
        return workspace, input_dir, output_dir, bridge, patches

    def test_pending_inferred_review_blocks_workbook_write(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(root)
            with patches[0], patches[1], patches[2]:
                pending = run_report(
                    workspace,
                    input_dir,
                    output_dir,
                    existing_state={"run_id": "synthetic-accept-run", "run_mappings": store_assignment_mapping()},
                )

                self.assertEqual(pending["status"], INFERRED_REVIEW)
                self.assertEqual(pending["review_batch"]["status"], INFERRED_REVIEW)
                self.assertTrue(all(item["status"] == "PENDING" for item in pending["review_batch"]["items"]))
                self.assertEqual(len(pending["review_batch"]["items"]), 1)
                item = pending["review_batch"]["items"][0]
                self.assertEqual(set(item["sources"]), SOURCES)
                self.assertEqual(item["proposed_answer"], PRODUCT)
                self.assertEqual(item["decision_type"], INFERRED_REVIEW)
                bridge.write.assert_not_called()
                self.assertFalse(output_dir.exists())

                paths = RuntimePaths.for_workspace(workspace)
                run_dir = paths.runs_dir / pending["run_id"]
                self.assertFalse((run_dir / "run-summary.json").exists())
                self.assertTrue(paths.current_run.exists())
                recoverable = json.loads(paths.current_run.read_text(encoding="utf-8"))
                self.assertEqual(recoverable["run_id"], pending["run_id"])
                self.assertEqual(recoverable["status"], INFERRED_REVIEW)
                self.assertFalse(paths.memory.exists())
                memory = LocalMemory(paths)
                for source in SOURCES:
                    self.assertIsNone(memory.resolve("campaign", source))

                completed = resolve_review_batch(workspace, reply_text="全部接受")

            self.assertEqual(bridge.write.call_count, 1)
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertIsNotNone(completed["output_path"])
            self.assertTrue(Path(completed["output_path"]).exists())
            self.assertTrue((run_dir / "run-summary.json").exists())
            confirmation = json.loads(paths.confirmations.read_text(encoding="utf-8").strip())
            self.assertEqual(confirmation["decision_type"], "HUMAN_CONFIRMED")
            self.assertEqual(confirmation["action"], "ACCEPT")
            self.assertEqual(confirmation["final_answer"], PRODUCT)
            self.assertEqual(confirmation["classification"], "RUN_ONLY")
            for source in SOURCES:
                self.assertIsNone(LocalMemory(paths).resolve("campaign", source))

    def test_reject_without_correction_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(root)
            with patches[0], patches[1], patches[2]:
                pending = run_report(
                    workspace,
                    input_dir,
                    output_dir,
                    existing_state={"run_id": "synthetic-reject-run", "run_mappings": store_assignment_mapping()},
                )
                review_id = pending["review_batch"]["items"][0]["review_id"]
                unresolved = resolve_review_batch(
                    workspace,
                    [{"review_id": review_id, "action": "REJECT", "persistence": "RUN_ONLY"}],
                )

            self.assertEqual(unresolved["status"], "HUMAN_REQUIRED")
            self.assertIsNone(unresolved["review_batch"])
            self.assertTrue(unresolved["gates"])
            bridge.write.assert_not_called()
            self.assertFalse(output_dir.exists())
            paths = RuntimePaths.for_workspace(workspace)
            self.assertFalse((paths.runs_dir / unresolved["run_id"] / "run-summary.json").exists())
            for source in SOURCES:
                self.assertIsNone(LocalMemory(paths).resolve("campaign", source))


if __name__ == "__main__":
    unittest.main()
