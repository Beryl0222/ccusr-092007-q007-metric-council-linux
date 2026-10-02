import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council.derivations import DerivedMetric, DeriveOp
from metric_council.disputes import DisputeRegistry
from metric_council.errors import (
    IncompatibleDefinitionsError,
    ValidationError,
    WorkflowError,
)
from metric_council.proposals import (
    DedupBoundary,
    EffectiveRange,
    ProposalStatus,
)
from _helpers import make_proposal


def approved(rid, code, **kw):
    return make_proposal(
        rid, metric_code=code, status=ProposalStatus.APPROVED, **kw
    )


class DerivationTest(unittest.TestCase):
    def test_compatible_components_sum(self):
        a = approved("ra", "A")
        b = approved("rb", "B", definition="另一个同口径收入")
        d = DerivedMetric("d1", "合计", DeriveOp.SUM, ("A", "B"), "CNY_10k_YUAN")
        d.validate_against({"A": a, "B": b})
        self.assertAlmostEqual(d.compute({"A": 100.0, "B": 50.0}), 150.0)

    def test_conflicting_components_rejected(self):
        a = approved("ra", "A", unit_code="CNY_10k_YUAN")
        b = approved(
            "rb", "B",
            unit_code="PERSON_VISIT",
            dedup=DedupBoundary("visit", ("ticket_id",), "day"),
        )
        d = DerivedMetric("bad", "混加", DeriveOp.SUM, ("A", "B"), "CNY_10k_YUAN")
        with self.assertRaises(IncompatibleDefinitionsError) as ctx:
            d.validate_against({"A": a, "B": b})
        self.assertEqual(ctx.exception.left, "A")
        self.assertEqual(ctx.exception.right, "B")

    def test_weighted_requires_weights_sum_one(self):
        a = approved("ra", "A")
        b = approved("rb", "B")
        d = DerivedMetric(
            "w", "加权", DeriveOp.WEIGHTED, ("A", "B"), "CNY_10k_YUAN",
            weights=(0.3, 0.6),
        )
        with self.assertRaises(ValidationError):
            d.validate_against({"A": a, "B": b})
        good = DerivedMetric(
            "w", "加权", DeriveOp.WEIGHTED, ("A", "B"), "CNY_10k_YUAN",
            weights=(0.3, 0.7),
        )
        good.validate_against({"A": a, "B": b})
        self.assertAlmostEqual(good.compute({"A": 100.0, "B": 200.0}), 170.0)

    def test_ratio_allows_cross_family_but_requires_ratio_unit(self):
        income = approved("ra", "INCOME", unit_code="CNY_10k_YUAN")
        visits = approved(
            "rb", "VISITS", unit_code="PERSON_VISIT",
            dedup=DedupBoundary("visit", ("ticket",), "event"),
        )
        bad = DerivedMetric("r0", "客单价?", DeriveOp.RATIO,
                            ("INCOME", "VISITS"), "CNY_10k_YUAN")
        with self.assertRaises(ValidationError):
            bad.validate_against({"INCOME": income, "VISITS": visits})
        ok = DerivedMetric("r1", "单次消费", DeriveOp.RATIO,
                           ("INCOME", "VISITS"), "RATIO")
        ok.validate_against({"INCOME": income, "VISITS": visits})

    def test_unapproved_component_rejected(self):
        draft = make_proposal("ra", metric_code="A")
        d = DerivedMetric("d", "x", DeriveOp.SUM, ("A",), "CNY_10k_YUAN")
        with self.assertRaises(WorkflowError):
            d.validate_against({"A": draft})

    def test_missing_component_rejected(self):
        d = DerivedMetric("d", "x", DeriveOp.SUM, ("A",), "CNY_10k_YUAN")
        with self.assertRaises(ValidationError):
            d.validate_against({})


class DisputeRegistryTest(unittest.TestCase):
    def test_conflict_opens_dispute_and_cannot_silently_merge(self):
        a = approved("ra", "A", unit_code="CNY_10k_YUAN")
        b = approved(
            "rb", "B", unit_code="PERSON_VISIT",
            dedup=DedupBoundary("visit", ("t",), "day"),
        )
        registry = DisputeRegistry()
        record = registry.open_from_conflict("D1", "宽窄口径", a, b)
        self.assertTrue(record.open)
        self.assertEqual(len(record.sides), 2)
        self.assertTrue(any("单位族" in r for r in record.reasons))

        # 兼容定义不得登记为冲突
        c = approved("rc", "C")
        with self.assertRaises(WorkflowError):
            registry.open_from_conflict("D2", "误报", a, c)

    def test_resolution_requires_party_and_is_recorded(self):
        a = approved("ra", "A", unit_code="CNY_10k_YUAN")
        b = approved(
            "rb", "B", unit_code="PERSON_VISIT",
            dedup=DedupBoundary("visit", ("t",), "day"),
        )
        registry = DisputeRegistry()
        registry.open_from_conflict("D1", "t", a, b)
        with self.assertRaises(KeyError):
            registry.resolve("D1", "outsider", "非当事方胜出")
        record = registry.resolve("D1", "rb", "窄口径经修订后胜出")
        self.assertFalse(record.open)
        self.assertEqual(record.resolution.winning_proposal_id, "rb")
        with self.assertRaises(WorkflowError):
            registry.resolve("D1", "ra", "重复关闭")

    def test_duplicate_dispute_id_rejected(self):
        a = approved("ra", "A", unit_code="CNY_10k_YUAN")
        b = approved(
            "rb", "B", unit_code="PERSON_VISIT",
            dedup=DedupBoundary("visit", ("t",), "day"),
        )
        registry = DisputeRegistry()
        registry.open_from_conflict("D1", "t", a, b)
        with self.assertRaises(WorkflowError):
            registry.open_from_conflict("D1", "t2", a, b)


if __name__ == "__main__":
    unittest.main()
