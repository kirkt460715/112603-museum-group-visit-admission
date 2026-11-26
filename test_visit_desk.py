"""参访后台的 HTTP 合同测试：路由、状态码、错误区分与跨进程台账重放。"""

import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from visit_core import VisitLedger
from visit_desk import open_visit_desk

RECORDS = Path(__file__).with_name("records")


class VisitDeskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ledger_path = Path(cls.tmp.name) / "events.jsonl"
        cls.server = open_visit_desk(RECORDS, cls.ledger_path, 0)
        cls.thread = Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01})
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def call(self, method, suffix, payload=None):
        raw = payload if isinstance(payload, str) else None
        data = (raw.encode("utf-8") if raw is not None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None else None)
        request = Request(self.origin + suffix, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.headers.get_content_charset(), "utf-8")
            return response.status, json.load(response)

    def fail_call(self, method, suffix, payload=None):
        raw = payload if isinstance(payload, str) else None
        data = (raw.encode("utf-8") if raw is not None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None else None)
        request = Request(self.origin + suffix, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        return caught.exception.code, json.load(caught.exception)

    def test_people_and_sessions_boards(self):
        _, people = self.call("GET", "/people")
        ids = {row["person"] for row in people["people"]}
        self.assertEqual(ids, {*self.server.ledger.people})
        _, board = self.call("GET", "/sessions/press-3/board")
        self.assertEqual(board["station"], "studio")
        self.assertIn("p5", board["admitted"]["students"])
        self.assertIn("reschedule_options", board)

    def test_unknown_person_and_route_are_404(self):
        code, payload = self.fail_call("GET", "/people/nobody")
        self.assertEqual(code, 404)
        self.assertEqual(payload["error"], "unknown_person")
        code, payload = self.fail_call("GET", "/nope")
        self.assertEqual(code, 404)

    def test_entry_review_flow_with_reasons_and_direct_arrival(self):
        # 教师自行确认直达被拒（403）。
        code, payload = self.fail_call("POST", "/direct-arrivals", {
            "session": "press-2", "person": "p2", "by": "teacher"})
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"], "venue_confirmation_required")

        # 未核实纸凭条返回待决，不产生入场。
        _, pending = self.call("POST", "/entries/review", {"observation_id": "paper-1"})
        self.assertEqual(pending["decision"], "pending_verification")

        # 第二站门控：先补陪同，再暴露 p2 缺第一站也无例外的问题。
        self.call("POST", "/entries/review",
                  {"session": "press-2", "person": "a1", "tick": 8})
        code, payload = self.fail_call("POST", "/entries/review",
                                       {"session": "press-2", "person": "p2", "tick": 8})
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"], "studio_gate_closed")

        # p4 持馆方直达确认，陪同已在馆，可直接放行。
        _, admitted = self.call("POST", "/entries/review",
                                {"session": "press-2", "person": "p4", "tick": 9})
        self.assertEqual(admitted["decision"], "admitted")
        _, person = self.call("GET", "/people/p4")
        self.assertTrue(person["visits"]["studio"]["direct_allowed"])

    def test_paper_verification_endpoint_and_immutability(self):
        code, payload = self.fail_call("POST", "/paper/paper-1/verify",
                                       {"verified": True, "by": "teacher"})
        self.assertEqual(code, 403)
        _, decision = self.call("POST", "/paper/paper-1/verify",
                                {"verified": False, "by": "venue"})
        self.assertFalse(decision["verified"])
        code, payload = self.fail_call("POST", "/paper/paper-1/verify",
                                       {"verified": True, "by": "venue"})
        self.assertEqual(payload["error"], "paper_already_decided")
        # 已查实的扫码器到场仍在。
        _, person = self.call("GET", "/people/p3")
        self.assertEqual(person["visits"]["gallery"]["admission"]["source"], "scanner")

    def test_split_confirmation_and_host_transfer(self):
        code, _ = self.fail_call("POST", "/splits/change-33/confirm", {"by": "teacher"})
        self.assertEqual(code, 403)
        _, split = self.call("POST", "/splits/change-33/confirm", {"by": "venue"})
        self.assertEqual(split["moves"], {"p3": "press-3"})
        _, person = self.call("GET", "/people/p3")
        self.assertEqual(person["visits"]["studio"]["session"], "press-3")
        # 已在 studio 入场的 p5 不能再被交接。
        code, payload = self.fail_call("POST", "/host-transfers", {
            "person": "p5", "session": "press-2", "taking_adult": "a1",
            "tick": 13, "by": "venue"})
        self.assertEqual(payload["error"], "already_admitted")
        # 迟到的 p6 由 north 的 a1 接待。
        _, transfer = self.call("POST", "/host-transfers", {
            "person": "p6", "session": "press-2", "taking_adult": "a1",
            "tick": 7, "by": "venue"})
        self.assertEqual(transfer["moves"], {"p6": "press-2"})

    def test_sponsorship_and_finance_reports(self):
        _, report = self.call("GET", "/sponsorship?grant=grant-5")
        self.assertEqual(report["grant"], "grant-5")
        self.assertEqual(report["payable_cents"], 0)  # 还没人完成两站
        _, finance = self.call("GET", "/finance/north")
        self.assertEqual(set(finance["calibers"]),
                         {"student_self_pay", "chaperone", "sponsor_payable"})
        refund_lines = [line for line in finance["lines"] if line.get("refund")]
        # p3 已自费但当前尚未完成两站，暂不产生退款。
        self.assertEqual(refund_lines, [])

    def test_bad_request_shapes(self):
        code, payload = self.fail_call("POST", "/entries/review", {})
        self.assertEqual(code, 400)
        self.assertIn("session", payload["fields"])
        code, payload = self.fail_call("POST", "/entries/review", "not-json")
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "bad_json")
        code, _ = self.fail_call("POST", "/paper/missing/verify",
                                 {"verified": True, "by": "venue"})
        self.assertEqual(code, 404)

    def test_ledger_replays_across_a_new_server(self):
        _, events = self.call("GET", "/ledger")
        written = [json.loads(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(written), len(events["events"]))
        rebuilt = VisitLedger(RECORDS, self.ledger_path)
        # 拆队/交接事件在重放后恢复同样的改约投影。
        for (station, person), session in self.server.ledger.overrides.items():
            self.assertEqual(rebuilt.assignment(station, person), session)
        # 纸凭条结论按台账实际内容恢复（该测试可能在核实用例之前或之后运行）。
        self.assertEqual(rebuilt.paper_status, self.server.ledger.paper_status)
        self.assertEqual(rebuilt.admissions[("gallery", "p3")]["source"], "scanner")
        # 重放不会把事件再写一遍。
        again = VisitLedger(RECORDS, self.ledger_path)
        self.assertEqual(len(again.events), len(written))


if __name__ == "__main__":
    unittest.main()
