"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import Conflict, InvalidState, QuotaExceeded
from .schedule import ScheduleService
from .service import PhotonService


def _error_status(exc: Exception) -> int:
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, KeyError):
        return 404
    if isinstance(exc, (Conflict, InvalidState, QuotaExceeded)):
        return 409
    return 400


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    schedule = ScheduleService(auth=service.auth)

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    @staticmethod
    def _query(path: str) -> dict:
        return {key: values[0] for key, values in parse_qs(urlparse(path).query).items()}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/health":
                return self._json(200, {"status": "ok", "service": "photon-fab"})
            if path.startswith("/lots/"):
                try:
                    return self._json(200, self.service.get_lot(self._token(), path.split("/", 2)[2]))
                except Exception as exc:
                    return self._json(_error_status(exc), {"error": str(exc)})
            token = self._token()
            query = self._query(self.path)
            if path == "/teams":
                return self._json(200, {"teams": self.schedule.list_teams(token)})
            if len(parts) == 2 and parts[0] == "teams":
                return self._json(200, self.schedule.get_team(token, parts[1]))
            if path == "/resources":
                return self._json(
                    200,
                    {"resources": self.schedule.list_resources(token, query.get("include_inactive") == "1")},
                )
            if len(parts) == 2 and parts[0] == "resources":
                return self._json(200, self.schedule.get_resource(token, parts[1]))
            if path == "/bookings":
                bookings = self.schedule.list_bookings(
                    token,
                    resource_id=query.get("resource_id"),
                    team_id=query.get("team_id"),
                    starts_after=query.get("starts_after"),
                    starts_before=query.get("starts_before"),
                    include_cancelled=query.get("include_cancelled") == "1",
                )
                return self._json(200, {"bookings": bookings})
            if len(parts) == 2 and parts[0] == "bookings":
                return self._json(200, self.schedule.get_booking(token, parts[1]))
            if path == "/quota":
                report = self.schedule.quota_report(
                    token, team_id=query.get("team_id"), week_of=query.get("week_of")
                )
                return self._json(200, {"report": report})
            if path == "/audit":
                events = self.schedule.audit_events(
                    token,
                    entity_type=query.get("entity_type"),
                    entity_id=query.get("entity_id"),
                    limit=int(query.get("limit", "200")),
                )
                return self._json(200, {"events": events})
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._json(_error_status(exc), {"error": str(exc)})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            parts = [p for p in self.path.split("/") if p]
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            if self.path == "/teams":
                result = self.schedule.create_team(
                    token, body["team_id"], body["name"], int(body.get("weekly_quota_minutes", 0))
                )
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "quota":
                return self._json(200, self.schedule.set_quota(token, parts[1], int(body["weekly_quota_minutes"])))
            if len(parts) == 3 and parts[0] == "teams" and parts[2] == "members":
                return self._json(201, self.schedule.add_member(token, body["user_id"], parts[1]))
            if self.path == "/resources":
                return self._json(201, self.schedule.create_resource(token, body["resource_id"], body["name"], body["kind"]))
            if self.path == "/bookings":
                result = self.schedule.create_booking(
                    token,
                    body["resource_id"],
                    body["starts_at"],
                    body["ends_at"],
                    team_id=body.get("team_id"),
                )
                return self._json(201, result)
            if len(parts) == 3 and parts[0] == "bookings" and parts[2] == "cancel":
                return self._json(200, self.schedule.cancel_booking(token, parts[1], body.get("reason", "")))
            if len(parts) == 3 and parts[0] == "bookings" and parts[2] == "change":
                return self._json(
                    200,
                    self.schedule.change_booking(token, parts[1], body["starts_at"], body["ends_at"]),
                )
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except Exception as exc:
            return self._json(_error_status(exc), {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    Handler.schedule = ScheduleService(auth=Handler.service.auth)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
