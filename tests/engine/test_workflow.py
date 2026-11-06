import copy
import unittest

from deskweave.domain.workflow import validate_workflow
from tests.engine.test_service import workflow


class WorkflowTests(unittest.TestCase):
    def codes(self, definition):
        return {row["code"] for row in validate_workflow(definition)}

    def test_valid_sample(self):
        self.assertEqual(validate_workflow(workflow()), [])

    def test_duplicate_missing_and_unreachable_nodes(self):
        value = workflow()
        value["nodes"].append(copy.deepcopy(value["nodes"][0]))
        self.assertIn("DUPLICATE_NODE", self.codes(value))
        value = workflow()
        value["entry"] = "missing"
        self.assertIn("ENTRY_NOT_FOUND", self.codes(value))
        value = workflow()
        value["nodes"][0]["on"]["next"] = None
        self.assertIn("UNREACHABLE_NODE", self.codes(value))

    def test_cycles_and_missing_outlets(self):
        value = workflow()
        value["nodes"][1]["on"]["next"] = value["nodes"][0]["id"]
        self.assertIn("GRAPH_CYCLE", self.codes(value))
        value["nodes"][1]["on"]["next"] = "missing"
        self.assertIn("OUTLET_NOT_FOUND", self.codes(value))

    def test_default_type_and_missing_required_field(self):
        value = workflow()
        value["parameters"]["counter"] = {
            "type": "integer",
            "required": False,
            "sensitive": False,
            "default": True,
        }
        self.assertIn("PARAMETER_TYPE", self.codes(value))
        del value["name"]
        self.assertEqual(self.codes(value), {"WORKFLOW_SCHEMA"})

    def test_deep_acyclic_graph(self):
        value = workflow()
        value["entry"] = "n0"
        value["nodes"] = [
            {
                "id": f"n{i}",
                "title": f"节点 {i}",
                "action": "time.wait",
                "params": {"durationMs": 0},
                "on": {"next": f"n{i + 1}" if i < 999 else None},
            }
            for i in range(1000)
        ]
        self.assertEqual(validate_workflow(value), [])

    def test_schema_diagnostics_do_not_assign_malformed_nodes(self):
        for nodes in (None, {}, [None], ["invalid node"]):
            with self.subTest(nodes=nodes):
                definition = workflow()
                definition["nodes"] = nodes
                diagnostics = validate_workflow(definition)
                self.assertTrue(diagnostics)
                self.assertTrue(all("nodeId" not in item for item in diagnostics))
                self.assertTrue(all(item["fieldPath"].startswith("/nodes") for item in diagnostics))
