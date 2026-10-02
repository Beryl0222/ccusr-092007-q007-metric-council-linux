import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from metric_council.proposals import (
    DedupBoundary,
    EffectiveRange,
    ProposalStatus,
    are_compatible,
    compatibility_report,
    load_proposal,
    proposal_from_payload,
)
from metric_council.errors import ValidationError
from _helpers import FIXTURE, make_proposal, proposal_payload


class ProposalContractTest(unittest.TestCase):
    def test_fixture_loads_as_full_proposal(self):
        proposal = load_proposal(FIXTURE)
        self.assertEqual(proposal.domain, "metric_council")
        self.assertEqual(proposal.record_id, "sample-007")
        self.assertEqual(proposal.revision, 1)
        self.assertEqual(proposal.metric_code, "CUL_CONS_WIDE")
        self.assertEqual(proposal.unit_code, "CNY_10k_YUAN")
        self.assertEqual(len(proposal.sources), 2)
        self.assertEqual(proposal.dedup.window, "month")
        self.assertEqual(proposal.status, ProposalStatus.DRAFT)

    def test_round_trip_payload(self):
        proposal = load_proposal(FIXTURE)
        again = proposal_from_payload(proposal.to_payload())
        self.assertEqual(again, proposal)

    def test_envelope_reader_ignores_new_fields(self):
        # 既有最小合同：load_record 只取信封，不因新字段报错
        from metric_council import load_record

        record = load_record(FIXTURE)
        self.assertEqual(record.record_id, "sample-007")
        self.assertEqual(record.domain, "metric_council")
        self.assertGreater(record.revision, 0)
        self.assertFalse(hasattr(record, "metric_code"))

    def test_validation_requires_core_fields(self):
        bad = make_proposal().__class__(
            record_id="x",
            occurred_at=None,
            revision=1,
            source="s",
            metric_code="",
            metric_name="",
            definition="",
            unit_code="CNY_10k_YUAN",
            industries=frozenset(),
            sources=(),
            dedup=None,
            effective=None,
        )
        with self.assertRaises(ValidationError):
            bad.validate()

    def test_effective_range_semantics(self):
        rng = EffectiveRange(date(2025, 1, 1), date(2026, 1, 1))
        self.assertTrue(rng.covers(date(2025, 12, 31)))
        self.assertFalse(rng.covers(date(2026, 1, 1)))
        self.assertTrue(EffectiveRange(date(2026, 1, 1)).covers(date(2030, 1, 1)))
        self.assertFalse(rng.overlaps(EffectiveRange(date(2026, 1, 1), date(2027, 1, 1))))
        self.assertTrue(rng.overlaps(EffectiveRange(date(2025, 6, 1), date(2027, 1, 1))))


class StatusMachineTest(unittest.TestCase):
    def test_legal_path(self):
        p = make_proposal()
        p = p.with_status(ProposalStatus.SUBMITTED)
        p = p.with_status(ProposalStatus.APPROVED)
        self.assertEqual(p.status, ProposalStatus.APPROVED)

    def test_cannot_skip_submission(self):
        p = make_proposal()
        with self.assertRaises(ValidationError):
            p.with_status(ProposalStatus.APPROVED)

    def test_terminal_states_immutable(self):
        for terminal in (ProposalStatus.APPROVED, ProposalStatus.REJECTED,
                         ProposalStatus.DISPUTED):
            p = make_proposal(status=terminal)
            with self.assertRaises(ValidationError):
                p.with_status(ProposalStatus.SUBMITTED)


class CompatibilityTest(unittest.TestCase):
    def test_compatible_same_boundary(self):
        a = make_proposal("a", metric_code="A")
        b = make_proposal("b", metric_code="B", industries=("digital_exhibition",))
        self.assertTrue(are_compatible(a, b))

    def test_incompatible_unit_family(self):
        a = make_proposal("a", metric_code="A", unit_code="CNY_10k_YUAN")
        b = make_proposal("b", metric_code="B", unit_code="PERSON_VISIT")
        ok, reasons = compatibility_report(a, b)
        self.assertFalse(ok)
        self.assertTrue(any("单位族" in r for r in reasons))

    def test_incompatible_dedup_window(self):
        a = make_proposal("a", metric_code="A")
        b = make_proposal(
            "b", metric_code="B",
            dedup=DedupBoundary("visit", ("ticket_id",), "day"),
        )
        ok, reasons = compatibility_report(a, b)
        self.assertFalse(ok)
        self.assertTrue(any("去重边界" in r for r in reasons))

    def test_revision_same_code_tolerates_industry_difference(self):
        # 同一指标的修订版本，行业口径调整不构成不兼容
        a = make_proposal("a", metric_code="SAME", industries=("x",))
        b = make_proposal("b", metric_code="SAME", revision=2, industries=("y",))
        self.assertTrue(are_compatible(a, b))


if __name__ == "__main__":
    unittest.main()
