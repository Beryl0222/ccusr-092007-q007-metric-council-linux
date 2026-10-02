import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from metric_council.api import create_app
from metric_council.service import CouncilService
from _helpers import proposal_payload


def call(app, method, path, body=None, *, actor_id="p1", role="proposer"):
    if "?" in path:
        path, query = path.split("?", 1)
    else:
        query = ""
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
        "HTTP_X_ACTOR_ID": actor_id,
        "HTTP_X_ACTOR_ROLE": role,
    }
    result = {}

    def start_response(status, headers):
        result["status"] = status

    data = b"".join(app(environ, start_response))
    return result["status"], json.loads(data.decode("utf-8"))


class ApiFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(CouncilService())

    def test_end_to_end_over_http(self):
        payload = proposal_payload()

        # 1. 业务部门提交
        status, out = call(self.app, "POST", "/api/proposals", payload)
        self.assertEqual(status, "201 Created")
        rid = out["record_id"]

        # 匿名被拒
        status, out = call(self.app, "POST", "/api/proposals", payload,
                           actor_id="", role="")
        self.assertEqual(status, "403 Forbidden")

        # 2. 统计专家 s1 回避；s2 与财政、行业表决
        status, out = call(
            self.app, "POST", f"/api/proposals/{rid}/recusal",
            {"relation": "本人在相关平台兼职"}, actor_id="s1", role="stat_reviewer",
        )
        self.assertEqual(status, "201 Created")
        status, out = call(
            self.app, "POST", f"/api/proposals/{rid}/opinions",
            {"impact": "统计口径可比", "vote": "approve"},
            actor_id="s2", role="stat_reviewer",
        )
        self.assertEqual(status, "201 Created")
        # s1 试图投票 → 409
        status, out = call(
            self.app, "POST", f"/api/proposals/{rid}/opinions",
            {"impact": "x", "vote": "approve"},
            actor_id="s1", role="stat_reviewer",
        )
        self.assertEqual(status, "409 Conflict")

        for actor_id, role in (("f1", "fiscal_reviewer"), ("i1", "industry_reviewer")):
            status, out = call(
                self.app, "POST", f"/api/proposals/{rid}/opinions",
                {"impact": "无异议", "vote": "approve"},
                actor_id=actor_id, role=role,
            )
            self.assertEqual(status, "201 Created")

        status, out = call(
            self.app, "POST", f"/api/proposals/{rid}/close", {},
            actor_id="s2", role="stat_reviewer",
        )
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["recusals"], 1)

        # 3. 序列登记与上报（亿元，服务自动换算到万元标准单位）
        status, out = call(self.app, "POST", "/api/series", {
            "metric_code": "CUL_CONS_WIDE",
            "canonical_unit": "CNY_10k_YUAN",
        })
        self.assertEqual(status, "201 Created")

        status, out = call(self.app, "POST", "/api/series/CUL_CONS_WIDE/ingest", {
            "batch": {
                "batch_id": "B1", "source_id": "ds-platform-monthly",
                "received_at": "2026-02-10T10:00:00+08:00",
                "submitted_by": "p1",
            },
            "rows": [{
                "region": "330100", "period": "2026-01",
                "as_of": "2026-01-31", "value": 1.0,
                "unit": "CNY_100M_YUAN", "level": 1,
            }],
        })
        self.assertEqual(status, "201 Created")
        self.assertEqual(out["version"], 1)

        # 企业明细被边界拒收
        status, out = call(self.app, "POST", "/api/series/CUL_CONS_WIDE/ingest", {
            "batch": {
                "batch_id": "B9", "source_id": "ds-x",
                "received_at": "2026-02-11T10:00:00+08:00",
                "submitted_by": "p1",
            },
            "rows": [{
                "region": "330100", "period": "2026-02",
                "as_of": "2026-02-28", "value": 1.0,
                "unit": "CNY_100M_YUAN", "level": 1,
                "enterprise_name": "不该出现的公司名",
            }],
        })
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("企业明细", out["error"])

        # 4. 发布
        status, snap = call(self.app, "POST", "/api/snapshots", {
            "snapshot_id": "R1",
            "report_name": "2026 年度监测报告",
            "selections": {"CUL_CONS_WIDE": []},
        }, actor_id="pub", role="publisher")
        self.assertEqual(status, "201 Created")
        self.assertEqual(snap["metric_versions"]["CUL_CONS_WIDE"], 1)

        # 5. 决策者取值与血缘；1 亿元 = 10000 万元
        status, out = call(
            self.app, "GET",
            "/api/snapshots/R1/value?metric=CUL_CONS_WIDE&region=330100&period=2026-01",
            actor_id="dm", role="decision_maker",
        )
        self.assertEqual(out["value"], 10000.0)

        status, out = call(
            self.app, "GET",
            "/api/snapshots/R1/lineage?metric=CUL_CONS_WIDE&region=330100&period=2026-01",
            actor_id="dm", role="decision_maker",
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(out["definition"]["dedup_boundary"]["window"], "month")
        self.assertEqual(len(out["review"]["recusals"]), 1)
        self.assertEqual(out["review"]["recusals"][0]["reviewer"], "s1")

        # 业务部门不能读血缘
        status, out = call(
            self.app, "GET",
            "/api/snapshots/R1/lineage?metric=CUL_CONS_WIDE&region=330100&period=2026-01",
        )
        self.assertEqual(status, "403 Forbidden")


if __name__ == "__main__":
    unittest.main()
