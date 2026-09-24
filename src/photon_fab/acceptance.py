"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .schedule import ScheduleService
from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    admin = service.auth.login("admin", "photon-admin")
    service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(admin, "LOT-DEMO")
    service.approve(admin, "LOT-DEMO", "hold", "awaiting quality review")

    schedule = ScheduleService(auth=service.auth)
    service.auth.create_user("eng", "eng-pass-123", "engineer")
    schedule.create_team(admin, "team-opto", "光电子器件组", weekly_quota_minutes=240)
    schedule.create_resource(admin, "spec-1", "共享光谱仪 A", "spectrometer")
    schedule.add_member(admin, "eng", "team-opto")
    engineer = service.auth.login("eng", "eng-pass-123")
    booking = schedule.create_booking(
        engineer, "spec-1", "2026-10-01T01:00:00Z", "2026-10-01T02:30:00Z"
    )
    report = schedule.quota_report(admin, "team-opto", week_of="2026-10-01T00:00:00Z")[0]
    schedule.cancel_booking(engineer, booking["booking_id"], "测量计划调整")
    events = schedule.audit_events(admin, entity_type="booking", entity_id=booking["booking_id"])
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(admin, "LOT-DEMO")),
        "booking_id": booking["booking_id"],
        "quota_booked_minutes": report["booked_minutes"],
        "booking_audit_events": len(events),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
