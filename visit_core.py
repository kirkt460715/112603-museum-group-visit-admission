"""馆校组合参访的领域内核：报名投影、两馆放行、拆队改约与代付核销。

设计约定：

- ``records/`` 三个来源是各经办方的原始观察，只读；内核把它们投影成
  人员、场次、资助合同等基础状态。
- 后台自己作出的每一个业务决定（放行、驳回、纸凭条核实、馆方例外确认、
  拆队/跨校接待确认）都作为事件追加到 JSONL 台账，重放即可复原，
  已查实的到场因此不可被后来的操作抹掉。
- 业务时刻一律用 tick（同一活动日的 15 分钟刻度）；事件落盘的墙钟时间
  只是登记时间，不参与任何业务判定。
"""

import json
import math
import threading
from pathlib import Path

GALLERY = "gallery"
STUDIO = "studio"
STATIONS = (GALLERY, STUDIO)

VENUE = "venue"


class VisitError(Exception):
    """业务规则违例。``code`` 供调用方区分情形，``status`` 是建议的 HTTP 状态。"""

    def __init__(self, code, message, status=409, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def as_payload(self):
        payload = {"error": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


def _load_feed(directory, name):
    path = directory / f"{name}.json"
    if not path.exists():
        raise VisitError("missing_feed", f"缺少来源登记 {name}", status=500)
    with path.open(encoding="utf-8") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list):
        raise VisitError("bad_feed", f"{name} 需要一组记录", status=500)
    return rows


class VisitLedger:
    def __init__(self, records_dir, ledger_path=None):
        self.records_dir = Path(records_dir)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.lock = threading.RLock()
        self._load_sources()
        self._reset_mutable_state()
        self._seed_from_sources()
        self._seq = 0
        if self.ledger_path and self.ledger_path.exists():
            self._replay()

    # ------------------------------------------------------------------ 来源装载

    def _load_sources(self):
        self.group_records = _load_feed(self.records_dir, "groups")
        self.venue_records = _load_feed(self.records_dir, "venues")
        self.sponsor_records = _load_feed(self.records_dir, "sponsorship")

    def _reset_mutable_state(self):
        self.people = {}            # person -> {group, school, role}
        self.groups = {}            # group -> 报名记录
        self.sessions = {}          # session -> 场次参数
        self.grants = {}            # grant -> 合同
        self.payments = []          # 票款记录（保持来源顺序）
        self.observations = {}      # observation id -> 原始观察
        self.change_requests = {}   # 拆队请求 id -> 请求记录

        self.overrides = {}         # (station, person) -> 改约后的 session
        self.direct_permissions = {}  # (session, person) -> 确认信息
        self.admissions = {}        # (station, person) -> 到场记录
        self.paper_status = {}      # observation id -> True/False；缺省为待核实
        self.reviews = []           # 全部放行判定（含驳回），按时间顺序
        self.confirmations = []     # 拆队/跨校接待确认事件
        self.events = []            # 已落盘事件

    def _seed_from_sources(self):
        # 报名：学生名额与陪同名额分别登记。
        for row in self.group_records:
            if "students" not in row:
                self.change_requests[row["id"]] = row
                continue
            self.groups[row["id"]] = row
            for person in row["students"]:
                self.people[person] = {"group": row["id"], "school": row["school"], "role": "student"}
            for person in row["adults"]:
                self.people[person] = {"group": row["id"], "school": row["school"], "role": "adult"}

        # 场馆：场次容量、到场观察、直达例外。
        for row in self.venue_records:
            if "admit" in row:
                self.sessions[row["id"]] = row
            elif row.get("kind") == "direct_arrival_allowed":
                if row.get("confirmed_by") != VENUE:
                    continue
                self.direct_permissions[(row["station"], row["person"])] = {
                    "source_id": row["id"], "confirmed_by": VENUE,
                }
            else:
                self.observations[row["id"]] = row

        for row in self.sponsor_records:
            if "eligible" in row:
                self.grants[row["id"]] = row
            else:
                self.payments.append(row)

        # 馆方扫码器已核实的记录就是既成到场；纸凭条保持待核实。
        for obs_id, obs in self.observations.items():
            if obs.get("source") == "scanner" and obs.get("verified"):
                self._admit(obs["station"], obs["person"], obs["tick"],
                            source="scanner", observation_id=obs_id, provenance="seed")

    # ------------------------------------------------------------------ 事件台账

    def _replay(self):
        with self.ledger_path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    event = json.loads(line)
                    self._seq = max(self._seq, event["seq"])
                    self._apply(event, persist=False, replay=True)

    def _record(self, event_type, payload):
        """业务方法已完成状态变更，这里只编号、登记并落盘，不重复应用。"""
        event = {"seq": self._seq + 1, "type": event_type, "payload": payload}
        self._persist(event)
        return event

    def _persist(self, event):
        self.events.append(event)
        self._seq = event["seq"]
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _apply(self, event, persist, replay=False):
        payload = event["payload"]
        kind = event["type"]
        # 重放的是后台自己已经作出的决定，按事实装载，只保留重复入场校验。
        provenance = "replay" if replay else "event"
        if kind == "entry_admitted":
            self._admit(payload["session"], payload["person"], payload["tick"],
                        source=payload.get("source", "desk"),
                        observation_id=payload.get("observation_id"),
                        provenance=provenance, record_event=False)
        elif kind == "entry_rejected":
            if replay:
                self.reviews.append(payload)
        elif kind == "paper_verified":
            if replay:
                self.paper_status[payload["observation_id"]] = payload["verified"]
            # 核实通过衍生的放行决定有自己的 entry_* 事件，重放时不在这里补发。
        elif kind == "direct_confirmed":
            if replay:
                self.direct_permissions[(payload["session"], payload["person"])] = {
                    "source_id": payload.get("source_id"), "confirmed_by": payload["by"],
                }
        elif kind in ("split_confirmed", "host_transfer_confirmed"):
            if replay:
                for person, session in payload["moves"].items():
                    station = self._station_of_session(session)
                    self.overrides[(station, person)] = session
                self.confirmations.append(payload)
        else:  # pragma: no cover - 未知事件直接失败，避免静默吞掉台账损坏
            raise VisitError("bad_event", f"未知台账事件 {kind}", status=500)

        if persist:
            self._persist(event)
        elif replay:
            self.events.append(event)
            self._seq = event["seq"]

    # ------------------------------------------------------------------ 基础查询

    def _station_of_session(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            raise VisitError("unknown_session", f"场次 {session_id} 不存在", status=404)
        return session["station"]

    def _require_person(self, person):
        if person not in self.people:
            raise VisitError("unknown_person", f"人员 {person} 未报名", status=404)
        return self.people[person]

    def _require_session(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            raise VisitError("unknown_session", f"场次 {session_id} 不存在", status=404)
        return session

    def assignment(self, station, person):
        """某人在某一站当前应走的场次：报名默认值叠加馆方确认的改约。"""
        if (station, person) in self.overrides:
            return self.overrides[(station, person)]
        group = self.groups[self.people[person]["group"]]
        return group["station_one"] if station == GALLERY else group["station_two"]

    def roster(self, session_id):
        station = self._station_of_session(session_id)
        students, adults = [], []
        for person, info in self.people.items():
            if self.assignment(station, person) == session_id:
                (students if info["role"] == "student" else adults).append(person)
        return sorted(students), sorted(adults)

    def admitted_at(self, session_id):
        students, adults = [], []
        for (station, person), entry in self.admissions.items():
            if entry["session"] == session_id:
                (students if self.people[person]["role"] == "student" else adults).append(person)
        return sorted(students), sorted(adults)

    def free_student_seats(self, session_id):
        session = self._require_session(session_id)
        students, _ = self.admitted_at(session_id)
        return session["capacity"] - len(students)

    # ------------------------------------------------------------------ 入场放行

    def _admit(self, session_id, person, tick, source, observation_id=None,
               provenance="event", record_event=True):
        session = self._require_session(session_id)
        station = session["station"]
        self._require_person(person)
        existing = self.admissions.get((station, person))
        if existing:
            raise VisitError("duplicate_entry",
                             f"{person} 在 {station} 已有到场记录",
                             existing=existing)
        # 已核实的扫码器/离线记录是既成事实，只做重复入场校验；
        # 时段、归属、容量、陪同比例、第一站门控都是现场放行决策的规则。
        if provenance == "event":
            lo, hi = session["admit"]
            if not lo <= tick <= hi:
                raise VisitError("outside_window",
                                 f"tick {tick} 不在场次 {session_id} 入场时段 [{lo}, {hi}] 内",
                                 session=session_id, window=[lo, hi])
            if self.assignment(station, person) != session_id:
                raise VisitError("not_assigned",
                                 f"{person} 当前不属于场次 {session_id}",
                                 assigned=self.assignment(station, person))
            if self.people[person]["role"] == "student":
                if len(self.admitted_at(session_id)[0]) >= session["capacity"]:
                    raise VisitError("session_full", f"场次 {session_id} 活动席位已满",
                                     **{"reschedule": self.reschedule_options(session_id)})
            if station == STUDIO and self.people[person]["role"] == "student":
                ratio = session.get("students_per_adult")
                admitted_students = len(self.admitted_at(session_id)[0]) + 1
                admitted_adults = len(self.admitted_at(session_id)[1])
                # 每名成人可带 ratio 名学生：组内学生在一名成人的名额内可先入场，
                # 超出下一档名额（如第 4 名）时必须先补成人。
                if ratio and admitted_adults < math.ceil(admitted_students / ratio):
                    raise VisitError("not_enough_adults",
                                     f"场次 {session_id} 陪同成人不足：每 {ratio} 名学生需 1 名成人",
                                     required_adults=math.ceil(admitted_students / ratio),
                                     admitted_adults=admitted_adults)
                if not self._studio_gate_ok(session_id, person):
                    raise VisitError("studio_gate_closed",
                                     f"{person} 缺少第一站已核实到场，也没有馆方直达确认")
        entry = {
            "station": station, "session": session_id, "person": person,
            "tick": tick, "source": source, "observation_id": observation_id,
            "provenance": provenance,
        }
        self.admissions[(station, person)] = entry
        self.reviews.append({**entry, "decision": "admitted"})
        if record_event and provenance == "event":
            self._record("entry_admitted", entry)
        return entry

    def _studio_gate_ok(self, session_id, person):
        # 直达放行与资助承认是两件事：这里只认第一站已核实到场或馆方例外。
        if (GALLERY, person) in self.admissions:
            return True
        return (session_id, person) in self.direct_permissions

    def review_entry(self, session_id, person, tick=None, observation_id=None):
        """售票员现场放行：可以凭人+时刻登记，也可以挂接一条离线观察。"""
        with self.lock:
            if observation_id is not None:
                obs = self.observations.get(observation_id)
                if not obs:
                    raise VisitError("unknown_observation",
                                     f"观察 {observation_id} 不存在", status=404)
                session_id, person, tick = obs["station"], obs["person"], obs["tick"]
                if obs.get("source") == "paper" and not self.paper_status.get(observation_id):
                    # 未核实纸凭条保持待决，绝不给第二站自动背书。
                    decision = {
                        "station": obs["station"], "person": person, "tick": tick,
                        "observation_id": observation_id,
                        "decision": "pending_verification", "reason": "paper_unverified",
                    }
                    self.reviews.append(decision)
                    self._record("entry_rejected", decision)
                    return decision
            if tick is None:
                raise VisitError("missing_tick", "现场登记需要实际 tick", status=400)
            try:
                source = self.observations[observation_id].get("source", "desk") \
                    if observation_id is not None else "desk"
                entry = self._admit(session_id, person, tick,
                                    source=source, observation_id=observation_id)
                return {"decision": "admitted", **entry}
            except VisitError as exc:
                decision = {
                    "station": self._require_session(session_id)["station"],
                    "session": session_id, "person": person, "tick": tick,
                    "observation_id": observation_id,
                    "decision": "rejected", "reason": exc.code,
                }
                self.reviews.append(decision)
                self._record("entry_rejected", decision)
                raise

    def verify_paper(self, observation_id, verified, by):
        """恢复联网后核实纸凭条；结论只增不改，已查实到场不受影响。"""
        with self.lock:
            obs = self.observations.get(observation_id)
            if not obs or obs.get("source") != "paper":
                raise VisitError("unknown_paper", f"纸凭条 {observation_id} 不存在", status=404)
            if by != VENUE:
                raise VisitError("venue_confirmation_required",
                                 "纸凭条核实只能由馆方作出", status=403)
            if observation_id in self.paper_status:
                raise VisitError("paper_already_decided",
                                 "纸凭条已有核实结论，不能覆盖",
                                 decision=self.paper_status[observation_id])
            event = self._record("paper_verified", {
                "observation_id": observation_id, "verified": bool(verified), "by": by,
            })
            self.paper_status[observation_id] = bool(verified)
            if verified:                # 核实通过后再走完整放行决策（容量/陪同/重复入场同样适用）；
                # 任何驳回都会作为 entry_rejected 事件留痕，不影响纸凭条结论。
                try:
                    self.review_entry(obs["station"], obs["person"], obs["tick"],
                                      observation_id=observation_id)
                except VisitError:
                    pass  # 驳回决定已由 review_entry 落事件，纸凭条核实结论仍然成立
            return event["payload"]

    def confirm_direct_arrival(self, session_id, person, by):
        """第一站例外直达第二站，只能由馆方确认，教师无权自行登记。"""
        with self.lock:
            self._require_session(session_id)
            self._require_person(person)
            if by != VENUE:
                raise VisitError("venue_confirmation_required",
                                 "直达例外必须由馆方确认", status=403)
            key = (session_id, person)
            if key in self.direct_permissions:
                return {"already_confirmed": True, **self.direct_permissions[key]}
            event = self._record("direct_confirmed", {
                "session": session_id, "person": person, "by": by,
            })
            self.direct_permissions[key] = {
                "source_id": None, "confirmed_by": by,
            }
            return event["payload"]

    # ------------------------------------------------------------------ 拆队与跨校接待

    def _confirm_reassignment(self, kind, moves, taking_adult, tick, by, request_id=None,
                              group=None):
        if by != VENUE:
            raise VisitError("venue_confirmation_required",
                             "改约必须由馆方确认", status=403)
        if not moves:
            raise VisitError("empty_reassignment", "没有需要改约的人", status=400)
        stations = set()
        for person, session_id in moves.items():
            self._require_person(person)
            station = self._station_of_session(session_id)
            stations.add(station)
            if (station, person) in self.admissions:
                # 已入场者仍归原来的实际场次，不能再改约。
                raise VisitError("already_admitted",
                                 f"{person} 已在 {station} 入场，须保留原场次",
                                 person=person)
        if len(stations) != 1:
            raise VisitError("cross_station_move", "一次改约只能针对同一站的场次")
        station = stations.pop()

        # 试算改约后的名册：重新确认时段余量与陪同比例。
        proposed = dict(self.overrides)
        for person, session_id in moves.items():
            proposed[(station, person)] = session_id

        def assigned(session_id, role):
            return [p for p, info in self.people.items()
                    if info["role"] == role
                    and proposed.get((station, p),
                                     self.assignment(station, p)) == session_id]

        for session_id in {*moves.values(), *[self.assignment(station, p) for p in moves]}:
            session = self.sessions[session_id]
            student_count = len(assigned(session_id, "student"))
            adult_count = len(assigned(session_id, "adult"))
            if student_count > session["capacity"]:
                raise VisitError("session_full",
                                 f"改约后 {session_id} 超出 {session['capacity']} 个活动席位")
            ratio = session.get("students_per_adult")
            if ratio and student_count > adult_count * ratio:
                raise VisitError("not_enough_adults",
                                 f"改约后 {session_id} 陪同比例不足",
                                 required_adults=math.ceil(student_count / ratio),
                                 adults=adult_count)
        if taking_adult is not None:
            self._require_person(taking_adult)
            if self.people[taking_adult]["role"] != "adult":
                raise VisitError("bad_taking_adult", f"{taking_adult} 不是陪同成人",
                                 status=400)
        payload = {
            "kind": kind, "request_id": request_id, "group": group,
            "moves": dict(moves), "taking_adult": taking_adult,
            "tick": tick, "by": by,
        }
        # 校验通过后先在状态上落改约，再编号、登记并落盘，避免事件应用重复改状态。
        for person, session_id in moves.items():
            self.overrides[(station, person)] = session_id
        self.confirmations.append(payload)
        event_type = "split_confirmed" if kind == "split" else "host_transfer_confirmed"
        return self._record(event_type, payload)["payload"]

    def confirm_split(self, request_id, by, tick=None):
        """处理团体拆班请求：重新确认时段、容量和陪同比例。"""
        with self.lock:
            request = self.change_requests.get(request_id)
            if not request:
                raise VisitError("unknown_request", f"拆队请求 {request_id} 不存在",
                                 status=404)
            return self._confirm_reassignment(
                "split", {request["student"]: request["wants"]},
                taking_adult=request.get("taking_adult"),
                tick=tick if tick is not None else request["tick"],
                by=by, request_id=request_id, group=request["group"],
            )

    def confirm_host_transfer(self, person, session_id, taking_adult, tick, by,
                              from_group=None):
        """迟到学生由另一学校接待：同样需要馆方确认并满足陪同比例。"""
        with self.lock:
            info = self._require_person(person)
            return self._confirm_reassignment(
                "host_transfer", {person: session_id}, taking_adult, tick, by,
                group=from_group or info["group"],
            )

    # ------------------------------------------------------------------ 视图

    def person_view(self, person):
        self._require_person(person)
        visits = {}
        for station in STATIONS:
            session_id = self.assignment(station, person)
            visits[station] = {
                "session": session_id,
                "admission": self.admissions.get((station, person)),
                "direct_allowed": (session_id, person) in self.direct_permissions,
            }
        pending_papers = [
            obs_id for obs_id, obs in self.observations.items()
            if obs.get("person") == person and obs.get("source") == "paper"
            and not self.paper_status.get(obs_id)
        ]
        return {
            "person": person, **self.people[person],
            "visits": visits,
            "in_venue": sorted(station for station in STATIONS
                               if (station, person) in self.admissions),
            "pending_paper_observations": sorted(pending_papers),
        }

    def session_board(self, session_id):
        session = self._require_session(session_id)
        students, adults = self.roster(session_id)
        in_students, in_adults = self.admitted_at(session_id)
        return {
            "session": session_id, "station": session["station"],
            "admit_window": session["admit"], "capacity": session["capacity"],
            "students_per_adult": session.get("students_per_adult"),
            "assigned": {"students": students, "adults": adults},
            "admitted": {"students": in_students, "adults": in_adults},
            "free_student_seats": self.free_student_seats(session_id),
            "waiting": [p for p in students if p not in in_students],
            "reschedule_options": self.reschedule_options(session_id),
        }

    def reschedule_options(self, session_id):
        station = self._station_of_session(session_id)
        alternatives = []
        for other_id, other in self.sessions.items():
            if other["station"] == station and other_id != session_id:
                seats = self.free_student_seats(other_id)
                if seats > 0:
                    alternatives.append({"session": other_id, "free_student_seats": seats})
        movable = [p for p in self.roster(session_id)[0]
                   if (station, p) not in self.admissions]
        return {"same_station_sessions": alternatives, "movable_people": sorted(movable)}

    # ------------------------------------------------------------------ 核销
    def _grants_for(self, grant_id=None, school=None):
        if grant_id:
            grant = self.grants.get(grant_id)
            if not grant:
                raise VisitError("unknown_grant", f"资助合同 {grant_id} 不存在", status=404)
            return [grant]
        return [g for g in self.grants.values() if school is None or g["school"] == school]

    def sponsorship_view(self, grant_id=None, school=None):
        with self.lock:
            reports = []
            for grant in self._grants_for(grant_id, school):
                rows, payable_count = [], 0
                for person in grant["eligible"]:
                    completed = sorted(
                        station for station in grant["required_stations"]
                        if (station, person) in self.admissions
                    )
                    qualifies = completed == sorted(grant["required_stations"])
                    payable = qualifies and payable_count < grant["student_limit"]
                    if payable:
                        payable_count += 1
                    rows.append({
                        "person": person,
                        "completed_stations": completed,
                        "qualifies": qualifies,
                        "payable": payable,
                        "payable_cents": grant["per_visitor"] if payable else 0,
                        "note": None if qualifies else "未完成合同要求的全部站点",
                    })
                reports.append({
                    "grant": grant["id"], "school": grant["school"],
                    "required_stations": grant["required_stations"],
                    "student_limit": grant["student_limit"],
                    "per_visitor_cents": grant["per_visitor"],
                    "eligible_visitors": rows,
                    "qualifying_count": sum(1 for r in rows if r["qualifies"]),
                    "payable_count": payable_count,
                    "payable_cents": payable_count * grant["per_visitor"],
                })
            return reports if school is not None or grant_id is None else reports[0]

    def _payment_lines(self, payment):
        if "lines" in payment:
            return [{"person": line["person"], "paid_cents": line["cents"]}
                    for line in payment["lines"]]
        return [{"person": person, "paid_cents": None} for person in payment["people"]]

    def payment_view(self, school):
        with self.lock:
            payable_people = set()
            for report in self.sponsorship_view(school=school):
                payable_people.update(
                    row["person"] for row in report["eligible_visitors"] if row["payable"]
                )
            lines, totals = [], {
                "self_pay_student_cents": 0, "chaperone_cents": 0,
                "sponsor_payable_cents": 0, "refund_due_cents": 0,
            }
            for payment in self.payments:
                if payment["school"] != school:
                    continue
                lump = payment.get("cents")
                for line in self._payment_lines(payment):
                    person = line["person"]
                    role = self.people.get(person, {}).get("role")
                    paid = line["paid_cents"]
                    entry = {
                        "payment": payment["id"], "person": person, "role": role,
                        "paid_cents": paid, "kind": None,
                        "sponsor_cents": 0, "refund": None, "note": None,
                    }
                    if role == "adult":
                        entry["kind"] = "chaperone_ticket"
                        if paid:
                            totals["chaperone_cents"] += paid
                    elif person in payable_people:
                        entry["kind"] = "sponsor_slot"
                        grant = next(g for g in self.grants.values()
                                     if person in g["eligible"] and g["school"] == school)
                        entry["sponsor_cents"] = grant["per_visitor"]
                        totals["sponsor_payable_cents"] += grant["per_visitor"]
                        if paid is None:
                            entry["note"] = "打包票款无法按人归属，差额需人工拆分"
                        else:
                            # 学校先替候补学生自费、其后改走代付名额：票退给付款方学校。
                            entry["refund"] = {"to": school, "cents": paid}
                            totals["refund_due_cents"] += paid
                    else:
                        entry["kind"] = "student_self_pay"
                        if paid:
                            totals["self_pay_student_cents"] += paid
                    lines.append(entry)
                if lump is not None:
                    lines.append({
                        "payment": payment["id"], "person": None, "role": None,
                        "paid_cents": lump, "kind": "payment_total",
                        "sponsor_cents": 0, "refund": None,
                        "note": "来源仅给总额，按人金额见上方空 paid_cents 行",
                    })
            return {
                "school": school,
                "calibers": {
                    "student_self_pay": totals["self_pay_student_cents"],
                    "chaperone": totals["chaperone_cents"],
                    "sponsor_payable": totals["sponsor_payable_cents"],
                },
                "refund_due": {"to": school, "cents": totals["refund_due_cents"]},
                "lines": lines,
            }
