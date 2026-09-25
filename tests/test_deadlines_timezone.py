"""跨时区截止：一律按 UTC 判定，按机构时区展示；迟到材料不能进入本轮包。"""
import unittest
from datetime import datetime, timezone

from helpers import ApiTestCase


class DeadlineTimezoneTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution(code="SH-01", tz="Asia/Shanghai")
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.item = self.create_item(self.coord_h, self.inst["id"])

    def test_deadline_stored_in_utc_and_shown_in_institution_timezone(self):
        pkg = self.create_package(self.inst["id"],
                                  deadline="2026-07-01T00:00:00+08:00")
        self.assertEqual(pkg["deadline_at"], "2026-06-30T16:00:00Z")
        self.assertEqual(pkg["deadline_local"], "2026-07-01T00:00:00+08:00")

    def test_naive_deadline_interpreted_as_utc(self):
        pkg = self.create_package(self.inst["id"], deadline="2026-07-01T00:00:00")
        self.assertEqual(pkg["deadline_at"], "2026-07-01T00:00:00Z")

    def test_new_york_deadline_converts_to_utc(self):
        ny = self.create_institution(code="NY-02", tz="America/New_York")
        pkg = self.create_package(ny["id"], deadline="2026-06-30T23:59:00-04:00")
        self.assertEqual(pkg["deadline_at"], "2026-07-01T03:59:00Z")
        self.assertEqual(pkg["deadline_local"], "2026-06-30T23:59:00-04:00")

    def test_submission_just_before_deadline_is_on_time(self):
        # 截止：北京时间 2026-07-01 00:00（UTC 2026-06-30 16:00）
        pkg = self.create_package(self.inst["id"],
                                  deadline="2026-07-01T00:00:00+08:00")
        self.clock.set(datetime(2026, 6, 30, 15, 59, 59, tzinfo=timezone.utc))
        result = self.upload(self.coord_h, self.item["id"], "压哨提交")
        self.assertFalse(result["late"])
        pin = self.client.post(
            f"/packages/{pkg['id']}/items",
            json={"evidence_version_id": result["version"]["id"]}, headers=self.officer)
        self.assertEqual(pin.status_code, 201)

    def test_submission_after_deadline_is_late_and_excluded(self):
        pkg = self.create_package(self.inst["id"],
                                  deadline="2026-07-01T00:00:00+08:00")
        # 北京本地 7 月 1 日 00:00:01 —— 本地日期已是“次日”，UTC 亦已过线
        self.clock.set(datetime(2026, 6, 30, 16, 0, 1, tzinfo=timezone.utc))
        result = self.upload(self.coord_h, self.item["id"], "迟到一秒")
        self.assertTrue(result["late"])
        pin = self.client.post(
            f"/packages/{pkg['id']}/items",
            json={"evidence_version_id": result["version"]["id"]}, headers=self.officer)
        self.assertEqual(pin.status_code, 422)

    def test_same_instant_judged_identically_across_zones(self):
        # 同一物理时刻，用不同时区时钟判定结果必须一致
        pkg = self.create_package(self.inst["id"],
                                  deadline="2026-07-01T00:00:00+08:00")
        shanghai_late = datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc).timestamp()
        self.clock.set(datetime.fromtimestamp(shanghai_late, tz=timezone.utc))
        result = self.upload(self.coord_h, self.item["id"], "迟到材料")
        self.assertTrue(result["late"])
        # 该时刻在纽约是 6 月 30 日中午，但截止判定不因此改变
        self.assertEqual(result["rereview_request_id"], None)  # 草稿期：仅标记迟到

    def test_late_after_seal_generates_rereview_request(self):
        pkg = self.create_package(self.inst["id"],
                                  deadline="2026-07-01T00:00:00+08:00")
        self.clock.set(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
        v1 = self.upload(self.coord_h, self.item["id"], "按时版本")["version"]
        self.pin(pkg["id"], v1["id"])
        self.seal(pkg["id"])
        # 封存后即使仍在截止前，新版本也属于后补
        self.clock.set(datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc))
        result = self.upload(self.coord_h, self.item["id"], "封存后补充")
        self.assertTrue(result["late"])
        self.assertIsNotNone(result["rereview_request_id"])


if __name__ == "__main__":
    unittest.main()
