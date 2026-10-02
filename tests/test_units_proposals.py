"""单位换算与提案合同测试。"""

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from support import base_proposal

from metric_council.errors import (
    ProposalValidationError,
    UnitConversionError,
)
from metric_council.proposals import (
    SCHEMA_VERSION,
    from_envelope,
    proposal_from_dict,
)
from metric_council.units import convert, get

FIXTURE = Path(__file__).parents[1] / "fixtures" / "metric_proposal.json"


class UnitTest(unittest.TestCase):
    def test_same_dimension_scales(self):
        self.assertAlmostEqual(convert(1, "CNY_YI", "CNY_WAN"), 10000.0)
        self.assertAlmostEqual(convert(500000, "CNY_YUAN", "CNY_WAN"), 50.0)
        self.assertAlmostEqual(convert(3, "VISIT_WAN", "PERSON_VISIT"), 30000.0)

    def test_cross_dimension_rejected(self):
        with self.assertRaises(UnitConversionError):
            convert(1, "CNY_WAN", "PERSON_VISIT")

    def test_unknown_unit_rejected(self):
        with self.assertRaises(UnitConversionError):
            get("USD")


class ProposalContractTest(unittest.TestCase):
    def test_fixture_is_v2_and_loads(self):
        proposal = proposal_from_dict(
            __import__("json").loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(proposal.schema_version, SCHEMA_VERSION)
        self.assertEqual(proposal.code, "CULT_CONSUME")
        self.assertEqual(proposal.unit, "CNY_WAN")
        self.assertTrue(proposal.dedup.identity_keys)
        self.assertIn("combo_ticket_transport_segment",
                      proposal.dedup.exclusions)

    def test_missing_business_element_rejected(self):
        payload = base_proposal()
        del payload["definition"]
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(payload)

    def test_unknown_unit_rejected(self):
        payload = base_proposal()
        payload["record_id"] = "x-1"
        payload["unit"] = "USD"
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(payload)

    def test_empty_industries_rejected(self):
        payload = base_proposal()
        payload["industries"] = []
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(payload)

    def test_bad_effective_range_rejected(self):
        payload = base_proposal()
        payload["effective"] = {"start": "2026-12-31", "end": "2026-01-01"}
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(payload)

    def test_bad_source_granularity_rejected(self):
        payload = base_proposal()
        payload["data_sources"][0]["granularity"] = "household"
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(payload)

    def test_v1_envelope_cannot_be_silently_treated_as_proposal(self):
        envelope = {
            "schema_version": 1, "record_id": "old-1",
            "domain": "metric_council", "occurred_at": "2026-01-01T00:00:00+08:00",
            "revision": 1, "source": "旧系统",
        }
        with self.assertRaises(ProposalValidationError):
            proposal_from_dict(envelope)

    def test_v1_to_v2_migration_keeps_envelope_identity(self):
        business = {
            "code": "CULT_CONSUME", "name": "居民文化消费额",
            "definition": "经业务部门补齐的完整定义",
            "unit": "CNY_WAN", "industries": ["R90"],
            "data_sources": [{
                "source_id": "ds-1", "name": "来源", "granularity": "aggregated",
                "reporting_lag_days": 30, "industries": ["R90"]}],
            "dedup": {"identity_keys": ["region_code", "period"],
                      "hierarchy_rule": "bottom_up"},
            "effective": {"start": "2026-01-01"},
            "submitting_dept": "文化产业规划处",
        }
        proposal = from_envelope(
            record_id="old-1",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            revision=1, source="旧系统", **business)
        self.assertEqual(proposal.record_id, "old-1")     # 追溯链保留
        self.assertEqual(proposal.revision, 1)
        self.assertEqual(proposal.schema_version, 2)
        self.assertEqual(proposal.source, "旧系统")

    def test_effective_range_covers_semantics(self):
        payload = base_proposal()
        proposal = proposal_from_dict(payload)
        self.assertTrue(proposal.effective.covers(date(2026, 6, 1)))
        self.assertFalse(proposal.effective.covers(date(2025, 12, 31)))


if __name__ == "__main__":
    unittest.main()
