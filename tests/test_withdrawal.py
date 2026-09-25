"""材料撤回：草稿移除、评审中失效、已签发保留历史并触发复审。"""
import unittest

from helpers import ApiTestCase


class WithdrawalTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.admin = self.create_user("机构管理员", "institution_admin", self.inst["id"])
        self.admin_h = self.headers(self.admin["id"])
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.rev = self.create_user("评审", "reviewer")
        self.rev_h = self.headers(self.rev["id"])
        self.item = self.create_item(self.coord_h, self.inst["id"])
        self.v1 = self.upload(self.coord_h, self.item["id"], "大纲 v1")["version"]

    def _withdraw(self, version_id: str):
        return self.client.post(f"/evidence/versions/{version_id}/withdraw",
                                json={"reason": "内容有误"}, headers=self.admin_h)

    def test_withdraw_removes_from_draft_and_blocks_repin(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        resp = self._withdraw(self.v1["id"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "withdrawn")

        view = self.get_package(self.officer, pkg["id"])
        self.assertEqual(view["items"], [], "草稿包应移除已撤回材料")
        self.assertEqual(view["status"], "draft")

        repin = self.client.post(f"/packages/{pkg['id']}/items",
                                 json={"evidence_version_id": self.v1["id"]},
                                 headers=self.officer)
        self.assertEqual(repin.status_code, 422)

    def test_withdraw_invalidates_package_under_review(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], self.rev["id"])
        self._withdraw(self.v1["id"])

        view = self.get_package(self.officer, pkg["id"])
        self.assertEqual(view["status"], "invalidated")
        self.assertIn("材料撤回", view["invalidated_reason"])

        # 失效包上的评审与签发都被拒绝
        submit = self.client.post(f"/packages/{pkg['id']}/reviews",
                                  json={"recommendation": "approve", "comments": ""},
                                  headers=self.rev_h)
        self.assertEqual(submit.status_code, 409)
        decide = self.client.post(f"/packages/{pkg['id']}/decision",
                                  json={"outcome": "approved", "rationale": ""},
                                  headers=self.officer)
        self.assertEqual(decide.status_code, 409)

        # 可恢复：从失效包开启新一轮，撤回材料自动排除
        v2 = self.upload(self.coord_h, self.item["id"], "大纲 v2 修正")["version"]
        resp = self.client.post(f"/packages/{pkg['id']}/rereview",
                                json={"new_deadline_at": "2026-12-01T00:00:00+08:00"},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 201)
        new_pkg = resp.json()
        self.assertEqual(new_pkg["cycle"], 2)
        self.assertEqual([i["version_id"] for i in new_pkg["items"]], [v2["id"]])
        sealed = self.seal(new_pkg["id"])
        self.assertEqual(sealed["status"], "sealed")

    def test_withdraw_after_decision_preserves_history_and_requests_rereview(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        sealed = self.seal(pkg["id"])
        self.assign(pkg["id"], self.rev["id"])
        self.review(self.rev_h, pkg["id"])
        decision = self.decide(pkg["id"])

        self._withdraw(self.v1["id"])
        view = self.get_package(self.officer, pkg["id"])
        self.assertEqual(view["status"], "decided", "已签发结论不可改写")
        self.assertEqual(view["decision"]["decision_hash"], decision["decision_hash"])
        self.assertEqual(view["manifest_hash"], sealed["manifest_hash"])

        reqs = self.client.get(f"/packages/{pkg['id']}/rereview-requests",
                               headers=self.officer).json()["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["reason"], "withdrawal")

        # 历史内容仍可核验（质量官可取回原文与指纹）
        content = self.client.get(f"/evidence/versions/{self.v1['id']}/content",
                                  headers=self.officer)
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.headers["X-Content-Sha256"], self.v1["sha256"])

    def test_withdraw_is_idempotent(self):
        first = self._withdraw(self.v1["id"])
        second = self._withdraw(self.v1["id"])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["withdrawn_at"], second.json()["withdrawn_at"])

    def test_coordinator_cannot_withdraw(self):
        resp = self.client.post(f"/evidence/versions/{self.v1['id']}/withdraw",
                                json={"reason": "越权"}, headers=self.coord_h)
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
