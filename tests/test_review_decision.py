"""评审执行：指派、评审、异议与结论签发（结论固定到材料清单）。"""
import unittest

from helpers import ApiTestCase


class ReviewDecisionTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.rev1 = self.create_user("评审一", "reviewer")
        self.rev2 = self.create_user("评审二", "reviewer")
        self.rev1_h = self.headers(self.rev1["id"])
        self.rev2_h = self.headers(self.rev2["id"])
        item = self.create_item(self.coord_h, self.inst["id"])
        self.v1 = self.upload(self.coord_h, item["id"], "大纲 v1")["version"]
        self.pkg = self.create_package(self.inst["id"])
        self.pin(self.pkg["id"], self.v1["id"])
        self.sealed = self.seal(self.pkg["id"])

    def _two_assigned_reviews(self):
        self.assign(self.pkg["id"], self.rev1["id"])
        self.assign(self.pkg["id"], self.rev2["id"])
        self.review(self.rev1_h, self.pkg["id"], "approve")
        self.review(self.rev2_h, self.pkg["id"], "conditional", "建议补充")

    def test_full_cycle_decision_pins_manifest(self):
        self._two_assigned_reviews()
        decision = self.decide(self.pkg["id"])
        self.assertEqual(decision["manifest_hash"], self.sealed["manifest_hash"])
        self.assertTrue(decision["decision_hash"])

        pkg = self.get_package(self.officer, self.pkg["id"])
        self.assertEqual(pkg["status"], "decided")
        self.assertEqual(pkg["decision"]["decision_hash"], decision["decision_hash"])
        self.assertEqual(len(pkg["reviews"]), 2)  # 质量官可见全部评审

    def test_decision_requires_all_reviews(self):
        self.assign(self.pkg["id"], self.rev1["id"])
        self.assign(self.pkg["id"], self.rev2["id"])
        self.review(self.rev1_h, self.pkg["id"])
        resp = self.client.post(f"/packages/{self.pkg['id']}/decision",
                                json={"outcome": "approved", "rationale": "x"},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 409)
        self.assertIn(self.rev2["id"], resp.json()["error"]["details"]["pending_reviewers"])

    def test_decision_idempotent_and_immutable(self):
        self._two_assigned_reviews()
        first = self.decide(self.pkg["id"])
        # 相同内容重试 → 同一结论
        retry = self.client.post(f"/packages/{self.pkg['id']}/decision",
                                 json={"outcome": "approved", "rationale": "通过"},
                                 headers=self.officer)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first["id"])
        # 不同内容 → 拒绝篡改
        changed = self.client.post(f"/packages/{self.pkg['id']}/decision",
                                   json={"outcome": "rejected", "rationale": "改判"},
                                   headers=self.officer)
        self.assertEqual(changed.status_code, 409)

    def test_open_objection_blocks_decision(self):
        self._two_assigned_reviews()
        obj = self.client.post(f"/packages/{self.pkg['id']}/objections",
                               json={"reason": "企业反馈未覆盖两个学期"},
                               headers=self.coord_h)
        self.assertEqual(obj.status_code, 201)
        blocked = self.client.post(f"/packages/{self.pkg['id']}/decision",
                                   json={"outcome": "approved", "rationale": "x"},
                                   headers=self.officer)
        self.assertEqual(blocked.status_code, 409)

        resolved = self.client.post(f"/objections/{obj.json()['id']}/resolve",
                                    json={"outcome": "dismissed", "note": "材料齐全"},
                                    headers=self.officer)
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(self.decide(self.pkg["id"])["outcome"], "approved")

    def test_objection_deduped_by_reason(self):
        first = self.client.post(f"/packages/{self.pkg['id']}/objections",
                                 json={"reason": "同一理由"}, headers=self.coord_h)
        second = self.client.post(f"/packages/{self.pkg['id']}/objections",
                                  json={"reason": "同一理由"}, headers=self.coord_h)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_review_revision_allowed_until_decision(self):
        self.assign(self.pkg["id"], self.rev1["id"])
        self.review(self.rev1_h, self.pkg["id"], "reject", "初评不通过")
        revised = self.review(self.rev1_h, self.pkg["id"], "conditional", "复核后有条件")
        self.assertEqual(revised["recommendation"], "conditional")
        self.decide(self.pkg["id"], "conditional", "采纳")
        late = self.client.post(f"/packages/{self.pkg['id']}/reviews",
                                json={"recommendation": "approve", "comments": "改口"},
                                headers=self.rev1_h)
        self.assertEqual(late.status_code, 409)

    def test_non_reviewer_cannot_submit_review(self):
        self.assign(self.pkg["id"], self.rev1["id"])
        resp = self.client.post(f"/packages/{self.pkg['id']}/reviews",
                                json={"recommendation": "approve", "comments": ""},
                                headers=self.coord_h)
        self.assertEqual(resp.status_code, 403)

    def test_unassigned_reviewer_cannot_submit(self):
        resp = self.client.post(f"/packages/{self.pkg['id']}/reviews",
                                json={"recommendation": "approve", "comments": ""},
                                headers=self.rev1_h)
        self.assertEqual(resp.status_code, 403)

    def test_assign_requires_sealed_package(self):
        pkg = self.create_package(self.inst["id"], term="2026-autumn",
                                  deadline="2026-12-01T00:00:00+08:00")
        resp = self.client.post(f"/packages/{pkg['id']}/assignments",
                                json={"reviewer_id": self.rev1["id"]},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 409)

    def test_institution_sees_decision_but_not_review_details(self):
        self._two_assigned_reviews()
        self.decide(self.pkg["id"])
        view = self.get_package(self.coord_h, self.pkg["id"])
        self.assertEqual(view["decision"]["outcome"], "approved")
        self.assertEqual(view["reviews"], [])  # 最小披露：机构看不到评审个人意见


if __name__ == "__main__":
    unittest.main()
