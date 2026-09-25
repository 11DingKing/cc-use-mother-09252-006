"""幂等：Idempotency-Key 重放返回首个响应；同键不同体冲突；天然键去重。"""
import unittest

from helpers import ApiTestCase, b64


class IdempotencyTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])

    def test_idempotency_key_replays_first_response(self):
        payload = {"institution_id": self.inst["id"], "term": "2026-spring",
                   "kind": "syllabus", "title": "大纲", "sensitivity": "public"}
        headers = {**self.coord_h, "Idempotency-Key": "item-001"}
        first = self.client.post("/evidence/items", json=payload, headers=headers)
        replay = self.client.post("/evidence/items", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)  # 重放返回首个响应
        self.assertEqual(first.json(), replay.json())

    def test_same_key_with_different_payload_conflicts(self):
        headers = {**self.coord_h, "Idempotency-Key": "item-002"}
        base = {"institution_id": self.inst["id"], "term": "2026-spring",
                "kind": "syllabus", "title": "大纲A"}
        self.client.post("/evidence/items", json=base, headers=headers)
        conflict = self.client.post("/evidence/items",
                                    json={**base, "title": "大纲B"}, headers=headers)
        self.assertEqual(conflict.status_code, 409)

    def test_key_is_scoped_per_actor(self):
        other = self.create_user("协调员乙", "coordinator", self.inst["id"])
        base = {"institution_id": self.inst["id"], "term": "2026-spring", "kind": "other"}
        first = self.client.post("/evidence/items", json={**base, "title": "材料甲"},
                                 headers={**self.coord_h, "Idempotency-Key": "shared"})
        second = self.client.post("/evidence/items", json={**base, "title": "材料乙"},
                                  headers={**self.headers(other["id"]),
                                           "Idempotency-Key": "shared"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)  # 不同操作者互不影响
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_decision_replay_with_key(self):
        rev = self.create_user("评审", "reviewer")
        rev_h = self.headers(rev["id"])
        item = self.create_item(self.coord_h, self.inst["id"])
        v1 = self.upload(self.coord_h, item["id"], "内容")["version"]
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], rev["id"])
        self.review(rev_h, pkg["id"])
        headers = {**self.officer, "Idempotency-Key": "decision-1"}
        body = {"outcome": "approved", "rationale": "通过"}
        first = self.client.post(f"/packages/{pkg['id']}/decision", json=body,
                                 headers=headers)
        replay = self.client.post(f"/packages/{pkg['id']}/decision", json=body,
                                  headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.json()["decision_hash"], replay.json()["decision_hash"])

    def test_natural_idempotency_without_key(self):
        item = self.create_item(self.coord_h, self.inst["id"])
        first = self.client.post(f"/evidence/items/{item['id']}/versions",
                                 json={"content_base64": b64("重复内容")},
                                 headers=self.coord_h)
        second = self.client.post(f"/evidence/items/{item['id']}/versions",
                                  json={"content_base64": b64("重复内容")},
                                  headers=self.coord_h)
        self.assertEqual(first.json()["version"]["id"], second.json()["version"]["id"])
        self.assertFalse(second.json()["created"])


if __name__ == "__main__":
    unittest.main()
