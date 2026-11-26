"""参访业务后台：教师报名投影、两馆放行、拆队交接与代付核销的 HTTP 面。

只读来源仍由 ``desk_source.py`` 提供；本服务的每个写操作都会落入 JSONL 事件
台账（默认 ``ledger/visit_events.jsonl``），重启后重放恢复。只监听回环地址。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from visit_core import VisitError, VisitLedger


class VisitHandler(BaseHTTPRequestHandler):
    def reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise VisitError("bad_json", "请求体不是合法 JSON", status=400)
        if not isinstance(payload, dict):
            raise VisitError("bad_json", "请求体必须是 JSON 对象", status=400)
        return payload

    def require_fields(self, payload, fields):
        missing = [name for name in fields if name not in payload]
        if missing:
            raise VisitError("missing_fields", f"缺少字段：{', '.join(missing)}",
                             status=400, **{"fields": missing})

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        url = urlsplit(self.path)
        ledger = self.server.ledger
        parts = [p for p in url.path.split("/") if p]
        params = parse_qs(url.query)
        try:
            if url.path == "/people":
                self.reply(200, {"people": [ledger.person_view(p) for p in sorted(ledger.people)]})
            elif len(parts) == 2 and parts[0] == "people":
                self.reply(200, ledger.person_view(parts[1]))
            elif url.path == "/sessions":
                self.reply(200, {"sessions": [
                    ledger.session_board(session_id) for session_id in sorted(ledger.sessions)
                ]})
            elif len(parts) == 3 and parts[0] == "sessions" and parts[2] == "board":
                self.reply(200, ledger.session_board(parts[1]))
            elif url.path == "/sponsorship":
                grant = params.get("grant", [None])[0]
                school = params.get("school", [None])[0]
                result = ledger.sponsorship_view(grant_id=grant, school=school)
                self.reply(200, {"reports": result} if isinstance(result, list) else result)
            elif len(parts) == 2 and parts[0] == "finance":
                self.reply(200, ledger.payment_view(parts[1]))
            elif url.path == "/reviews":
                self.reply(200, {"reviews": ledger.reviews})
            elif url.path == "/ledger":
                self.reply(200, {"events": ledger.events})
            else:
                self.reply(404, {"error": "no_route", "message": "无此路由"})
        except VisitError as exc:
            self.reply(exc.status, exc.as_payload())

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        url = urlsplit(self.path)
        ledger = self.server.ledger
        parts = [p for p in url.path.split("/") if p]
        try:
            payload = self.read_json()
            if url.path == "/entries/review":
                if "observation_id" in payload:
                    result = ledger.review_entry(
                        None, None, observation_id=payload["observation_id"])
                else:
                    self.require_fields(payload, ["session", "person", "tick"])
                    result = ledger.review_entry(
                        payload["session"], payload["person"], tick=payload["tick"])
                self.reply(200, result)
            elif len(parts) == 3 and parts[0] == "paper" and parts[2] == "verify":
                self.require_fields(payload, ["verified", "by"])
                result = ledger.verify_paper(parts[1], payload["verified"], payload["by"])
                self.reply(200, result)
            elif url.path == "/direct-arrivals":
                self.require_fields(payload, ["session", "person", "by"])
                result = ledger.confirm_direct_arrival(
                    payload["session"], payload["person"], payload["by"])
                self.reply(200, result)
            elif len(parts) == 3 and parts[0] == "splits" and parts[2] == "confirm":
                self.require_fields(payload, ["by"])
                result = ledger.confirm_split(
                    parts[1], payload["by"], tick=payload.get("tick"))
                self.reply(200, result)
            elif url.path == "/host-transfers":
                self.require_fields(payload, ["person", "session", "taking_adult", "tick", "by"])
                result = ledger.confirm_host_transfer(
                    payload["person"], payload["session"], payload["taking_adult"],
                    payload["tick"], payload["by"], from_group=payload.get("from_group"))
                self.reply(200, result)
            else:
                self.reply(404, {"error": "no_route", "message": "无此路由"})
        except VisitError as exc:
            self.reply(exc.status, exc.as_payload())

    def log_message(self, format, *args):
        pass


def open_visit_desk(records_dir, ledger_path, port):
    ledger = VisitLedger(records_dir, ledger_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), VisitHandler)
    server.ledger = ledger
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--records", type=Path, default=Path(__file__).with_name("records"))
    parser.add_argument("--ledger", type=Path,
                        default=Path(__file__).with_name("ledger") / "visit_events.jsonl")
    options = parser.parse_args()
    with open_visit_desk(options.records, options.ledger, options.port) as server:
        print(f"参访后台已启动，端口 {server.server_port}，台账 {options.ledger}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
