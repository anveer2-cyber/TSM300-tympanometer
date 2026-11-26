"""Stand-ins for the clinic's systems: a records database in sqlite and a 상쾌한캡처 receiver."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-key"


def records_database(day: str = "2026-11-26") -> sqlite3.Connection:
    """A database shaped like the clinic's, holding made-up patients and that day's visits."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        CREATE TABLE zupsu (chart_code INTEGER, show_chart_code TEXT, susin_name TEXT,
                            jumin_number TEXT, birthday TEXT, hpon TEXT);
        CREATE TABLE exam_date (exam_date_code INTEGER, chart_code INTEGER, exam_date TEXT, jinro_room TEXT);
        CREATE TABLE exam_yak (exam_date_code INTEGER, yak_code TEXT);
    """)
    db.executemany("INSERT INTO zupsu VALUES (?, ?, ?, ?, ?, ?)", [
        (66000, "00512", "홍길동", "750304-1000000", "", "010-0000-1111"),
        (66001, "12345", "김하나", "800131-2000000", "", "010-0000-2222"),
        (66002, "24680", "박세나", "", "2015-03-02", ""),
        (66003, "77777", "동명일", "900101-1000000", "", ""),
        (66004, "77777", "동명이", "900202-2000000", "", ""),
    ])
    db.executemany("INSERT INTO exam_date VALUES (?, ?, ?, ?)", [
        (1, 66000, f"{day} 09:12:00", "진료1실"),
        (2, 66001, f"{day} 09:40:00", "진료2실"),
        (3, 66002, f"{day} 10:05:00", "진료1실"),
        (4, 99999, f"{day} 10:30:00", "진료1실"),
        (5, 66000, "2026-11-25 15:00:00", "진료1실"),
    ])
    db.executemany("INSERT INTO exam_yak VALUES (?, ?)", [
        (1, "F6361 "), (1, "F6362"), (1, "AA154"),
        (2, "F6311"),
        (3, "F6361"),
        (4, "F6361"),
        (5, "F6361"),
    ])
    return db


class CaptureReceiver:
    """Answers like 상쾌한캡처's result receiver, for one known patient."""

    def __init__(self, patient=("00512", "홍길동", "1975-03-04")) -> None:
        self.received: list[dict] = []
        self.fail_with: tuple[int, str, str] | None = None
        self._patient = patient
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _reply(self, status, body):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self):
                if self.headers.get("X-EntCapture-Key") == KEY:
                    return True
                self._reply(401, {"ok": False, "code": "bad_key", "error": "키가 맞지 않습니다"})
                return False

            def do_GET(self):
                if self._authorised():
                    self._reply(200, {"device": "중이검사실", "pc": "진료실PC"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._authorised():
                    return
                fields = _form_fields(self.headers.get("Content-Type", ""), body)
                self._reply(*receiver.accept(fields))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def accept(self, fields: dict) -> tuple[int, dict]:
        if self.fail_with:
            status, code, error = self.fail_with
            return status, {"ok": False, "code": code, "error": error}
        chart, name, birth = self._patient
        if fields.get("chart") != chart:
            return 404, {"ok": False, "code": "patient_not_found", "error": "그 차트번호의 환자가 없습니다"}
        if fields.get("name") != name:
            return 409, {"ok": False, "code": "name_mismatch", "error": "이름이 전자차트와 다릅니다"}
        if fields.get("birth") and fields["birth"] != birth:
            return 409, {"ok": False, "code": "birth_mismatch", "error": "생년월일이 전자차트와 다릅니다"}
        duplicate = any(sent["file"] == fields["file"] for sent in self.received)
        if not duplicate:
            self.received.append(fields)
        number = len(self.received)
        suffix = "" if number == 1 else f"_{number}"
        day = fields["date"].replace("-", "")
        return 200, {"ok": True, "duplicate": duplicate, "id": f"{number:032d}", "chartNo": chart,
                     "imageDataName": f"66000_{day}_중이검사{suffix}.jpg", "message": "저장했습니다"}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _form_fields(content_type: str, body: bytes) -> dict:
    boundary = content_type.split("boundary=")[1].encode()
    fields = {}
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, value = part.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        value = value[:-2]
        fields[name] = value if name == "file" else value.decode("utf-8")
    return fields


TOKEN = "relay-token"


class RelayServer:
    """Answers like the clinic's relay server's TSM300 addresses, from made-up records."""

    PATIENTS = [
        {"chart": "00512", "chart_code": "512", "name": "홍길동", "birth": "1975-03-04", "age": 51, "sex": "남",
         "last": "2026-11-26", "how": "차트번호"},
        {"chart": "12345", "chart_code": "", "name": "김하나", "birth": "1980-01-31", "age": 46, "sex": "여",
         "last": "2026-11-20", "how": "차트번호"},
    ]
    ORDERS = [
        {"chart": "12345", "chart_code": "", "name": "김하나", "birth": "1980-01-31", "age": 46, "sex": "여",
         "time": "09:40", "room": "진료2실", "codes": ["F6361"], "visit": 2},
        {"chart": "00512", "chart_code": "512", "name": "홍길동", "birth": "1975-03-04", "age": 51, "sex": "남",
         "time": "09:12", "room": "진료1실", "codes": ["F6361", "F6362"], "visit": 1, "added_later": "ignored"},
        {"chart": "", "chart_code": "", "name": "", "birth": "", "age": None, "sex": "",
         "time": "10:30", "room": "", "codes": ["F6361"], "visit": 3},
    ]

    def __init__(self, old_version: bool = False) -> None:
        self.requests: list[tuple[str, dict, str | None]] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _reply(self, status, body):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit

                parts = urlsplit(self.path)
                query = {name: values[0] for name, values in parse_qs(parts.query).items()}
                relay.requests.append((parts.path, query, self.headers.get("X-TSM-Token")))
                if old_version:
                    self.send_error(404)
                elif self.headers.get("X-TSM-Token") != TOKEN:
                    self._reply(401, {"ok": False, "error": "토큰이 맞지 않습니다"})
                elif parts.path == "/tsm/api/patients":
                    found = [p for p in relay.PATIENTS if p["chart"] == query.get("q")]
                    self._reply(200, {"ok": True, "patients": found})
                elif parts.path == "/tsm/api/orders":
                    day = query.get("day", "")
                    if len(day) != 10:
                        self._reply(400, {"ok": False, "error": "day 는 YYYY-MM-DD 여야 합니다"})
                    else:
                        self._reply(200, {"ok": True, "day": day, "orders": relay.ORDERS})
                else:
                    self._reply(404, {"ok": False, "error": "없는 주소입니다"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
