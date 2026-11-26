"""领域规则回归：覆盖叙述中的名额、放行、纸凭条、拆队与核销口径。"""

import json
import tempfile
import unittest
from pathlib import Path

from visit_core import VisitError, VisitLedger

RECORDS = Path(__file__).with_name("records")


def ledger():
    return VisitLedger(RECORDS)


class EnrollmentTest(unittest.TestCase):
    def setUp(self):
        self.ledger = ledger()

    def test_student_and_chaperone_quotas_are_separate_roles(self):
        self.assertEqual(self.ledger.people["p1"]["role"], "student")
        self.assertEqual(self.ledger.people["a1"]["role"], "adult")
        students, adults = self.ledger.roster("press-2")
        self.assertEqual(students, ["p1", "p2", "p3", "p4"])
        self.assertEqual(adults, ["a1", "a2"])

    def test_default_assignments_follow_the_group_booking(self):
        self.assertEqual(self.ledger.assignment("gallery", "p1"), "show-1")
        self.assertEqual(self.ledger.assignment("studio", "p5"), "press-3")
        view = self.ledger.person_view("p3")
        self.assertEqual(view["in_venue"], ["gallery"])  # 扫码器已核实的第一站

    def test_seed_scanner_record_is_verified_attendance_but_paper_stays_pending(self):
        entry = self.ledger.admissions[("gallery", "p3")]
        self.assertEqual(entry["source"], "scanner")
        self.assertEqual(entry["tick"], 6)
        self.assertNotIn(("studio", "p3"), self.ledger.admissions)
        self.assertEqual(self.ledger.person_view("p3")["pending_paper_observations"], ["paper-1"])
        # 种子里两笔已核实扫码器记录（gallery 的 p3、studio 的 p5）。
        self.assertEqual(sum(1 for e in self.ledger.admissions.values()
                             if e["source"] == "scanner"), 2)


class EntryGateTest(unittest.TestCase):
    def setUp(self):
        self.ledger = ledger()

    def test_studio_needs_gallery_or_venue_direct_permission(self):
        # p2 没去过第一站、也没有例外：即使陪同先入场，第二站仍不放行。
        self.ledger.review_entry("press-2", "a1", tick=8)
        with self.assertRaises(VisitError) as blocked:
            self.ledger.review_entry("press-2", "p2", tick=8)
        self.assertEqual(blocked.exception.code, "studio_gate_closed")
        self.assertIn({"station": "studio", "person": "p2", "decision": "rejected"},
                      [{"station": r["station"], "person": r["person"],
                        "decision": r["decision"]} for r in self.ledger.reviews])

    def test_venue_confirmed_direct_arrival_opens_only_studio_gate(self):
        # p4 的直达确认来自来源里的馆方记录；陪同先入，学生后入。
        self.ledger.review_entry("press-2", "a1", tick=8)
        self.ledger.review_entry("press-2", "p4", tick=8)
        self.assertEqual(self.ledger.admissions[("studio", "p4")]["source"], "desk")
        self.assertNotIn(("gallery", "p4"), self.ledger.admissions)

    def test_teacher_cannot_self_authorize_direct_arrival(self):
        with self.assertRaises(VisitError) as blocked:
            self.ledger.confirm_direct_arrival("press-2", "p1", by="teacher")
        self.assertEqual(blocked.exception.status, 403)

    def test_admission_window_is_enforced(self):
        with self.assertRaises(VisitError) as blocked:
            self.ledger.review_entry("show-1", "p1", tick=3)
        self.assertEqual(blocked.exception.code, "outside_window")
        self.ledger.review_entry("show-1", "p1", tick=7)  # 边界 tick 仍有效

    def test_duplicate_entry_is_caught_even_across_paper_and_scanner(self):
        self.assertEqual(len(self.ledger.admissions), 2)
        # p3 已有扫码器到场，纸凭条即便核实也不能制造第二次入场。
        self.ledger.verify_paper("paper-1", True, by="venue")
        reviews = [r for r in self.ledger.reviews if r.get("observation_id") == "paper-1"]
        self.assertTrue(any(r["reason"] == "duplicate_entry" for r in reviews))
        self.assertEqual(len(self.ledger.admissions), 2)  # 原两场到场没有被抹掉

    def test_unverified_paper_never_endorses_studio(self):
        # p3 在第一站已有核实到场，正常可进第二站；但用未核实纸凭条挂单只能待决。
        pending = self.ledger.review_entry(None, None, observation_id="paper-1")
        self.assertEqual(pending["decision"], "pending_verification")
        with self.assertRaises(VisitError) as blocked:
            self.ledger.review_entry("press-3", "p3", tick=12)
        self.assertEqual(blocked.exception.code, "not_assigned")

    def test_rejected_paper_keeps_verified_attendance_intact(self):
        self.ledger.verify_paper("paper-1", False, by="venue")
        self.assertEqual(self.ledger.paper_status["paper-1"], False)
        # 之前扫码器核实的第一站到场仍在。
        self.assertEqual(self.ledger.admissions[("gallery", "p3")]["observation_id"], "counter-34")
        with self.assertRaises(VisitError) as blocked:
            self.ledger.verify_paper("paper-1", True, by="venue")
        self.assertEqual(blocked.exception.code, "paper_already_decided")

    def test_paper_verification_requires_venue(self):
        with self.assertRaises(VisitError) as blocked:
            self.ledger.verify_paper("paper-1", True, by="teacher")
        self.assertEqual(blocked.exception.status, 403)

    def test_wrong_session_is_rejected(self):
        with self.assertRaises(VisitError) as blocked:
            self.ledger.review_entry("press-3", "p1", tick=12)
        self.assertEqual(blocked.exception.code, "not_assigned")


class RatioAndCapacityTest(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        directory = Path(tmp.name)
        groups = [{
            "id": "g1", "school": "north",
            "students": ["s1", "s2", "s3", "s4"], "adults": ["t1"],
            "station_one": "show", "station_two": "press",
        }]
        venues = [
            {"id": "show", "station": "gallery", "admit": [0, 20], "capacity": 20},
            {"id": "press", "station": "studio", "admit": [0, 20],
             "capacity": 5, "students_per_adult": 3},
        ]
        for name, rows in (("groups", groups), ("venues", venues), ("sponsorship", [])):
            (directory / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
        return tmp, VisitLedger(directory)

    def test_student_admission_waits_for_required_chaperones(self):
        tmp, book = self._fixture()
        self.addCleanup(tmp.cleanup)
        for student in ("s1", "s2", "s3", "s4"):
            book.admissions[("gallery", student)] = {"station": "gallery", "session": "show",
                                                     "person": student, "tick": 1}
        # 没有成人随组时，第一名学生也不能进第二站。
        with self.assertRaises(VisitError) as blocked:
            book.review_entry("press", "s1", tick=5)
        self.assertEqual(blocked.exception.code, "not_enough_adults")
        book.review_entry("press", "t1", tick=5)   # 成人先入
        book.review_entry("press", "s1", tick=5)
        book.review_entry("press", "s2", tick=5)
        book.review_entry("press", "s3", tick=5)   # 3 名学生配 1 名成人，正好
        with self.assertRaises(VisitError) as blocked:
            book.review_entry("press", "s4", tick=5)  # 第 4 名需要第二名成人
        self.assertEqual(blocked.exception.code, "not_enough_adults")
        self.assertEqual(blocked.exception.extra["required_adults"], 2)
        self.assertEqual(blocked.exception.extra["admitted_adults"], 1)

    def test_capacity_counts_student_seats_only(self):
        tmp, book = self._fixture()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(book.free_student_seats("press"), 5)
        book.review_entry("press", "t1", tick=5)
        self.assertEqual(book.free_student_seats("press"), 5)  # 陪同不占活动席位

    def test_full_session_lists_reschedule_and_movable_people(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        groups = [
            {"id": "g1", "school": "north", "students": ["s1"], "adults": ["t1"],
             "station_one": "show", "station_two": "press-a"},
            {"id": "g2", "school": "south", "students": ["s2", "s3"], "adults": ["t2"],
             "station_one": "show", "station_two": "press-b"},
        ]
        venues = [
            {"id": "show", "station": "gallery", "admit": [0, 20], "capacity": 20},
            {"id": "press-a", "station": "studio", "admit": [0, 20],
             "capacity": 1, "students_per_adult": 3},
            {"id": "press-b", "station": "studio", "admit": [0, 20],
             "capacity": 2, "students_per_adult": 3},
        ]
        for name, rows in (("groups", groups), ("venues", venues), ("sponsorship", [])):
            (directory / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
        book = VisitLedger(directory)
        for student in ("s1", "s2", "s3"):
            book.admissions[("gallery", student)] = {
                "station": "gallery", "session": "show", "person": student, "tick": 1}
        book.review_entry("press-a", "t1", tick=4)
        book.review_entry("press-a", "s1", tick=5)
        book.review_entry("press-b", "t2", tick=5)
        book.review_entry("press-b", "s2", tick=5)
        # s3 尚未入场：满座时它是可改约对象；入场后即被钉住。
        before = book.reschedule_options("press-b")
        self.assertEqual(before["movable_people"], ["s3"])
        book.review_entry("press-b", "s3", tick=5)  # 占满最后一个活动席位
        options = book.reschedule_options("press-b")
        self.assertEqual(options["same_station_sessions"], [])  # press-a 也满
        # 已入场者钉在实际场次，不能改约；但在馆名单可用于现场交接。
        self.assertEqual(options["movable_people"], [])
        board = book.session_board("press-b")
        self.assertEqual(board["free_student_seats"], 0)
        self.assertEqual(board["admitted"]["students"], ["s2", "s3"])


class ReassignmentTest(unittest.TestCase):
    def setUp(self):
        self.ledger = ledger()

    def test_split_request_needs_venue_confirmation_and_then_reassigns(self):
        with self.assertRaises(VisitError) as blocked:
            self.ledger.confirm_split("change-33", by="teacher")
        self.assertEqual(blocked.exception.status, 403)
        self.ledger.confirm_split("change-33", by="venue")
        self.assertEqual(self.ledger.assignment("studio", "p3"), "press-3")
        # 原场次的名册不再包含 p3，但已在第一站入场的记录仍归 show-1。
        self.assertNotIn("p3", self.ledger.roster("press-2")[0])
        self.assertEqual(self.ledger.admissions[("gallery", "p3")]["session"], "show-1")

    def test_admitted_person_is_pinned_to_actual_session(self):
        # p5 已在 press-3 入场，不能再被改约。
        with self.assertRaises(VisitError) as blocked:
            self.ledger.confirm_host_transfer("p5", "press-2", "a1", tick=13, by="venue")
        self.assertEqual(blocked.exception.code, "already_admitted")

    def test_host_transfer_for_late_student_carries_chaperone_and_ratio(self):
        book = self.ledger
        # p6 迟到，由 north 的 a1 在 press-2 接待。
        book.confirm_host_transfer("p6", "press-2", "a1", tick=7, by="venue")
        self.assertEqual(book.assignment("studio", "p6"), "press-2")
        students, adults = book.roster("press-2")
        self.assertIn("p6", students)
        self.assertIn("a1", adults)
        # press-3 名册里 p6 消失，陪同比例重新满足（剩 p5 一人，a3 一人）。
        self.assertNotIn("p6", book.roster("press-3")[0])

    def test_reassignment_that_breaks_ratio_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        groups = [
            {"id": "g1", "school": "north", "students": ["s1", "s2", "s3", "s4"],
             "adults": ["t1"], "station_one": "show", "station_two": "press-a"},
            {"id": "g2", "school": "south", "students": ["s5"], "adults": ["t2"],
             "station_one": "show", "station_two": "press-b"},
        ]
        venues = [
            {"id": "show", "station": "gallery", "admit": [0, 20], "capacity": 20},
            {"id": "press-a", "station": "studio", "admit": [0, 20],
             "capacity": 10, "students_per_adult": 2},
            {"id": "press-b", "station": "studio", "admit": [0, 20],
             "capacity": 10, "students_per_adult": 2},
        ]
        for name, rows in (("groups", groups), ("venues", venues), ("sponsorship", [])):
            (directory / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
        book = VisitLedger(directory)
        # 把 4 名学生塞进只有 1 名成人的 press-b（比例 2:1，最多 2 人）。
        with self.assertRaises(VisitError) as blocked:
            book.confirm_host_transfer("s1", "press-b", "t2", tick=5, by="venue")
        self.assertEqual(blocked.exception.code, "not_enough_adults")

    def test_reassignment_over_capacity_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        groups = [
            {"id": "g1", "school": "north", "students": ["s1", "s2"], "adults": ["t1"],
             "station_one": "show", "station_two": "press-a"},
        ]
        venues = [
            {"id": "show", "station": "gallery", "admit": [0, 20], "capacity": 20},
            {"id": "press-a", "station": "studio", "admit": [0, 20],
             "capacity": 10, "students_per_adult": 3},
            {"id": "press-b", "station": "studio", "admit": [0, 20],
             "capacity": 0, "students_per_adult": 3},
        ]
        for name, rows in (("groups", groups), ("venues", venues), ("sponsorship", [])):
            (directory / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
        book = VisitLedger(directory)
        with self.assertRaises(VisitError) as blocked:
            book.confirm_host_transfer("s1", "press-b", "t1", tick=5, by="venue")
        self.assertEqual(blocked.exception.code, "session_full")


class SponsorshipTest(unittest.TestCase):
    def setUp(self):
        self.ledger = ledger()

    def _complete(self, person, studio_session):
        if ("gallery", person) not in self.ledger.admissions:
            self.ledger.admissions[("gallery", person)] = {
                "station": "gallery", "session": "show-1", "person": person, "tick": 5}
        if ("studio", person) not in self.ledger.admissions:
            self.ledger.admissions[("studio", person)] = {
                "station": "studio", "session": studio_session, "person": person, "tick": 9}

    def test_only_contract_stations_count_and_limit_is_applied(self):
        # 先完成拆队，让 p3 的第二站归属 press-3；已入场者不能再改约。
        self.ledger.confirm_split("change-33", by="venue")
        # p1、p3、p4 合资格；p2 不在 eligible 名单。
        for person, studio in (("p1", "press-2"), ("p3", "press-3"), ("p4", "press-2")):
            self._complete(person, studio)
        reports = self.ledger.sponsorship_view(grant_id="grant-5")
        rows = {r["person"]: r for r in reports["eligible_visitors"]}
        self.assertTrue(all(rows[p]["qualifies"] for p in ("p1", "p3", "p4")))
        payable = [p for p, row in rows.items() if row["payable"]]
        self.assertEqual(len(payable), 2)  # student_limit = 2
        self.assertEqual(reports["payable_count"], 2)
        self.assertEqual(reports["payable_cents"], 4800)
        self.assertEqual(rows["p1"]["payable_cents"] + rows["p3"]["payable_cents"]
                         + rows["p4"]["payable_cents"], 4800)

    def test_missing_station_means_no_recognition_and_no_payment(self):
        # p4 只去第二站（直达例外），合同要求两站：直达有效但资助不承认。
        self.ledger.review_entry("press-2", "a1", tick=8)
        self.ledger.review_entry("press-2", "p4", tick=8)
        report = self.ledger.sponsorship_view(grant_id="grant-5")
        row = next(r for r in report["eligible_visitors"] if r["person"] == "p4")
        self.assertFalse(row["qualifies"])
        self.assertEqual(row["payable_cents"], 0)
        self.assertEqual(report["payable_cents"], 0)

    def test_finance_keeps_three_ticket_calibers_and_refunds_school(self):
        self.ledger.confirm_split("change-33", by="venue")
        self._complete("p1", "press-2")
        self._complete("p3", "press-3")
        view = self.ledger.payment_view("north")
        by_person = {line["person"]: line for line in view["lines"] if line["person"]}
        # p3 已自费 2400，又进入代付名额：差额退给学校，基金按 2400 核销。
        self.assertEqual(by_person["p3"]["kind"], "sponsor_slot")
        self.assertEqual(by_person["p3"]["sponsor_cents"], 2400)
        self.assertEqual(by_person["p3"]["refund"], {"to": "north", "cents": 2400})
        # 陪同票独立口径，不进资助。
        self.assertEqual(by_person["a2"]["kind"], "chaperone_ticket")
        self.assertEqual(view["calibers"]["chaperone"], 900)
        # p1 合资格但学生名额上限 2，本场景只有 p3 一人同时满足两站与在付名单条件。
        self.assertEqual(view["calibers"]["sponsor_payable"], 2400)
        self.assertEqual(view["refund_due"], {"to": "north", "cents": 2400})

    def test_lump_sum_payment_is_flagged_when_cannot_attribute_per_person(self):
        view = self.ledger.payment_view("south")
        lines = [line for line in view["lines"] if line["payment"] == "payment-10"]
        self.assertTrue(all(line["paid_cents"] is None for line in lines
                            if line["kind"] != "payment_total"))
        total = next(line for line in lines if line["kind"] == "payment_total")
        self.assertEqual(total["paid_cents"], 5700)

    def test_fund_never_pays_for_uncompleted_school(self):
        reports = self.ledger.sponsorship_view(school="north")
        self.assertEqual(reports[0]["payable_cents"], 0)


class LedgerPersistenceTest(unittest.TestCase):
    def test_replay_restores_decisions_but_seed_observations_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            first = VisitLedger(RECORDS, path)
            first.confirm_split("change-33", by="venue")
            first.verify_paper("paper-1", False, by="venue")
            first.review_entry("press-2", "a1", tick=8)
            first.review_entry("press-2", "p4", tick=8)
            second = VisitLedger(RECORDS, path)
            self.assertEqual(second.assignment("studio", "p3"), "press-3")
            self.assertEqual(second.paper_status["paper-1"], False)
            self.assertIn(("studio", "p4"), second.admissions)
            # 已查实的扫码器到场在重放后仍然存在。
            self.assertEqual(second.admissions[("gallery", "p3")]["source"], "scanner")
            # 事件只追加：重新打开不会重复写入种子观察。
            self.assertEqual(len(second.events), len(first.events))


if __name__ == "__main__":
    unittest.main()
