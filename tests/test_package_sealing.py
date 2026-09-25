"""评审包封装：封存固定材料清单，封存后不可改，后补文件只生成复审请求。"""
import unittest

from helpers import ApiTestCase


class PackageSealingTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.item = self.create_item(self.coord_h, self.inst["id"])
        self.v1 = self.upload(self.coord_h, self.item["id"], "大纲 v1")["version"]

    def test_seal_pins_manifest_and_is_idempotent(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        sealed = self.seal(pkg["id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertTrue(sealed["manifest_hash"])
        self.assertEqual(sealed["items"][0]["sha256"], self.v1["sha256"])

        again = self.seal(pkg["id"])  # 幂等：重复封存结果一致
        self.assertEqual(again["manifest_hash"], sealed["manifest_hash"])

    def test_cannot_modify_after_seal(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        self.seal(pkg["id"])
        v2 = self.upload(self.coord_h, self.item["id"], "大纲 v2")["version"]
        resp = self.client.post(f"/packages/{pkg['id']}/items",
                                json={"evidence_version_id": v2["id"]},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 409)

    def test_late_version_after_seal_only_creates_rereview_request(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        sealed = self.seal(pkg["id"])

        late = self.upload(self.coord_h, self.item["id"], "大纲 v2 后补")
        self.assertTrue(late["late"])
        self.assertIsNotNone(late["rereview_request_id"])

        # 已封存包的材料与清单指纹不受影响
        after = self.get_package(self.officer, pkg["id"])
        self.assertEqual(after["manifest_hash"], sealed["manifest_hash"])
        self.assertEqual(len(after["items"]), 1)
        self.assertEqual(after["open_rereview_requests"], 1)

        reqs = self.client.get(f"/packages/{pkg['id']}/rereview-requests",
                               headers=self.officer).json()["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["reason"], "late_submission")
        self.assertEqual(reqs[0]["status"], "open")

    def test_seal_rejects_empty_package(self):
        pkg = self.create_package(self.inst["id"])
        resp = self.client.post(f"/packages/{pkg['id']}/seal", headers=self.officer)
        self.assertEqual(resp.status_code, 422)

    def test_pin_rejects_foreign_term_material(self):
        other_item = self.create_item(self.coord_h, self.inst["id"],
                                      term="2026-autumn", title="秋季大纲")
        other_v = self.upload(self.coord_h, other_item["id"], "秋季内容")["version"]
        pkg = self.create_package(self.inst["id"], term="2026-spring")
        resp = self.client.post(f"/packages/{pkg['id']}/items",
                                json={"evidence_version_id": other_v["id"]},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
