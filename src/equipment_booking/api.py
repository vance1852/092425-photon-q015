"""设备预约的无第三方依赖 HTTP JSON 接口。

除 ``GET /health`` 外，所有请求都通过 ``X-Actor-Id`` 携带操作者编号；
时间字段一律使用带时区的 ISO 8601（建议直接以 ``Z`` 结尾的 UTC 时间）。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import BookingError, Unauthenticated, ValidationFailed
from .service import BookingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    def __init__(self, service: BookingService) -> None:
        self.service = service

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)

        def q(name: str, default: str | None = None) -> str | None:
            return query.get(name, [default])[0]

        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "equipment-booking"})

            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = normalized.get("x-actor-id", "").strip()
            # 仅首个用户建档允许匿名（初始化管理员）；其他接口一律要求 X-Actor-Id。
            if not actor and not (method == "POST" and path == "/users"):
                raise Unauthenticated("缺少 X-Actor-Id")

            if method == "POST" and path == "/teams":
                return Response(201, self.service.create_team(
                    actor, payload["team_id"], payload["name"],
                    int(payload.get("weekly_quota_minutes", 0)),
                ))
            if method == "GET" and path == "/teams":
                return Response(200, {"teams": self.service.teams(actor)})
            if method == "GET" and len(parts) == 2 and parts[0] == "teams":
                return Response(200, self.service.team(actor, parts[1]))
            if method == "PUT" and len(parts) == 3 and parts[0] == "teams" and parts[2] == "quota":
                return Response(200, self.service.set_weekly_quota(
                    actor, parts[1], int(payload["weekly_quota_minutes"])
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "teams" and parts[2] == "quota":
                return Response(200, self.service.weekly_quota_usage(actor, parts[1], q("week")))

            if method == "POST" and path == "/users":
                # 仅系统尚无任何用户（初始化）时允许匿名创建首个管理员。
                if not actor and self.service.has_users():
                    raise Unauthenticated("缺少 X-Actor-Id")
                return Response(201, self.service.create_user(
                    actor or payload["user_id"], payload["user_id"], payload["display_name"],
                    payload["team_id"], payload.get("role", "engineer"),
                ))

            if method == "POST" and path == "/resources":
                return Response(201, self.service.create_resource(
                    actor, payload["resource_id"], payload["name"], payload["kind"]
                ))
            if method == "GET" and path == "/resources":
                return Response(200, {"resources": self.service.resources(actor)})
            if method == "GET" and len(parts) == 2 and parts[0] == "resources":
                return Response(200, self.service.resource(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "resources" and parts[2] == "deactivate":
                return Response(200, self.service.deactivate_resource(
                    actor, parts[1], payload["reason"]
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "resources" and parts[2] == "schedule":
                return Response(200, self.service.schedule(
                    actor, parts[1], q("start"), q("end"),
                    q("include_cancelled", "") in {"1", "true", "yes"},
                ))

            if method == "POST" and path == "/reservations":
                return Response(201, self.service.create_reservation(
                    actor, payload["resource_id"], payload["starts_at"], payload["ends_at"],
                    payload["purpose"], payload.get("team_id"), payload.get("idempotency_key"),
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "reservations":
                return Response(200, self.service.reservation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "cancel":
                return Response(200, self.service.cancel_reservation(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "reschedule":
                return Response(200, self.service.reschedule_reservation(
                    actor, parts[1], payload["starts_at"], payload["ends_at"], payload.get("purpose")
                ))

            if method == "GET" and path == "/audit":
                return Response(200, {"events": self.service.audit_events(
                    actor, q("entity_type"), q("entity_id")
                )})
            if method == "GET" and path == "/audit/verify":
                return Response(200, self.service.verify_chain(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except BookingError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EquipmentBooking/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动实验室设备预约服务")
    parser.add_argument("--database", type=Path, default=Path("equipment_booking.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    service = BookingService(connection)
    service.bootstrap_admin()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(service)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
