from __future__ import annotations

from contextvars import ContextVar
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda, RunnableSequence
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled

from audit_core import orchestrator
from audit_core.common import AuditError
from audit_core import product_database
from audit_core.workflow import build_audit_chain, invoke_audit_chain


class WorkflowTests(unittest.TestCase):
    def test_catalog_snapshot_is_shared_across_nodes_but_not_across_runs(self):
        observed = []

        def stage(state):
            catalog = product_database.load_product_catalog()
            observed.append(catalog["version"])
            catalog["version"] = "caller mutation"
            return state

        with (
            patch.object(product_database, "_read_database_rows", return_value=[]) as read,
            patch.object(product_database, "catalog_from_rows", side_effect=[{"version": 1}, {"version": 2}]),
        ):
            for _run in range(2):
                with product_database.product_catalog_scope():
                    invoke_audit_chain(build_audit_chain([("intake", stage), ("decision", stage)]), {})
            self.assertEqual(read.call_count, 2)
        self.assertEqual(observed, [1, 1, 2, 2])

    def test_failure_stops_later_stages_without_replaying_successes(self):
        calls = []

        def fail(state):
            calls.append("analysis")
            raise AuditError("当前材料块失败")

        chain = build_audit_chain([
            ("intake", lambda state: calls.append("intake") or state),
            ("analysis", fail),
            ("evidence", lambda state: calls.append("evidence") or state),
        ])
        self.assertIsInstance(chain, RunnableSequence)
        with self.assertRaisesRegex(AuditError, "当前材料块失败"):
            invoke_audit_chain(chain, {})
        self.assertEqual(calls, ["intake", "analysis"])

    def test_tracing_is_local_and_context_restored_on_failure(self):
        scope = ContextVar("test_audit_catalog")
        scope.set("same-run-catalog")

        def stage(state):
            self.assertFalse(tracing_is_enabled())
            self.assertEqual(scope.get(), "same-run-catalog")
            raise AuditError("stop")

        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true"}), tracing_context(enabled=True):
            with self.assertRaises(AuditError):
                invoke_audit_chain(build_audit_chain([("analysis", stage), ("decision", Mock())]), {})
            self.assertTrue(tracing_is_enabled())
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "true")

    def test_outer_callbacks_do_not_receive_audit_stages_or_private_results(self):
        seen = []

        class Observer(BaseCallbackHandler):
            def on_chain_start(self, _serialized, inputs, **kwargs):
                seen.append((kwargs.get("name"), inputs))

        chain = build_audit_chain([
            ("analysis", lambda state: {"private_evidence": "must stay local"}),
            ("decision", lambda state: {**state, "report": "private report"}),
        ])
        with tracing_context(enabled=False):
            outer = RunnableLambda(lambda _value: invoke_audit_chain(chain, {}), name="outer")
            outer.invoke(None, config={"callbacks": [Observer()]})
        self.assertEqual(seen, [("outer", None)])

    def test_invalid_later_evidence_blocks_all_decisions_and_cleans_sources(self):
        from tests.pdf_test_support import bundle, PolicyProvider
        events = []
        def mutate(case, value):
            if case["kind"] == "pdf_policy_audit" and case["archive_id"] == "a002":
                value["checks"].pop()
        provider = PolicyProvider(("poster_material", "entry_fee"), mutate=mutate)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle(root / "input", "A.zip")
            bundle(root / "input", "B.zip")
            with self.assertRaisesRegex(AuditError, "全部且仅覆盖"):
                orchestrator.run_audit("20260917-chain-validation", producer_model="codex",
                    input_dir=root / "input", output_dir=root, evidence_provider=provider,
                    observer=lambda event, payload: events.append(event))
        self.assertEqual(len(provider.calls), 4)
        self.assertNotIn("result.validated", events)
        self.assertNotIn("report.verified", events)
        self.assertTrue(all(not root.exists() for root in provider.temporary_roots))


if __name__ == "__main__":
    unittest.main()
