"""证据接收：内容指纹、版本链与天然幂等。"""
import hashlib
import unittest

from helpers import ApiTestCase, b64


class EvidenceChainTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])

    def test_ingest_creates_fingerprinted_version_chain(self):
        item = self.create_item(self.coord_h, self.inst["id"])
        v1 = self.upload(self.coord_h, item["id"], "大纲第一版")["version"]
        v2 = self.upload(self.coord_h, item["id"], "大纲第二版")["version"]

        self.assertEqual(v1["seq"], 1)
        self.assertIsNone(v1["supersedes_id"])
        self.assertEqual(v2["seq"], 2)
        self.assertEqual(v2["supersedes_id"], v1["id"])
        self.assertEqual(v1["sha256"], hashlib.sha256("大纲第一版".encode()).hexdigest())
        self.assertEqual(v2["sha256"], hashlib.sha256("大纲第二版".encode()).hexdigest())

        detail = self.client.get(f"/evidence/items/{item['id']}", headers=self.coord_h)
        self.assertEqual([v["seq"] for v in detail.json()["versions"]], [1, 2])

    def test_same_content_is_idempotent(self):
        item = self.create_item(self.coord_h, self.inst["id"])
        first = self.client.post(f"/evidence/items/{item['id']}/versions",
                                 json={"content_base64": b64("相同内容")},
                                 headers=self.coord_h)
        second = self.client.post(f"/evidence/items/{item['id']}/versions",
                                  json={"content_base64": b64("相同内容")},
                                  headers=self.coord_h)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["version"]["id"], second.json()["version"]["id"])

        detail = self.client.get(f"/evidence/items/{item['id']}", headers=self.coord_h)
        self.assertEqual(len(detail.json()["versions"]), 1)

    def test_item_natural_key_is_idempotent(self):
        payload = {"institution_id": self.inst["id"], "term": "2026-spring",
                   "kind": "syllabus", "title": "课程大纲", "sensitivity": "public"}
        first = self.client.post("/evidence/items", json=payload, headers=self.coord_h)
        second = self.client.post("/evidence/items", json=payload, headers=self.coord_h)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_other_institution_cannot_submit(self):
        other = self.create_institution(code="NY-02", tz="America/New_York")
        resp = self.client.post(
            "/evidence/items",
            json={"institution_id": other["id"], "term": "2026-spring",
                  "kind": "syllabus", "title": "越权材料"},
            headers=self.coord_h)
        self.assertEqual(resp.status_code, 403)

    def test_invalid_kind_rejected(self):
        resp = self.client.post(
            "/evidence/items",
            json={"institution_id": self.inst["id"], "term": "2026-spring",
                  "kind": "unknown", "title": "x"},
            headers=self.coord_h)
        self.assertEqual(resp.status_code, 422)

    def test_unauthenticated_rejected(self):
        resp = self.client.post(
            "/evidence/items",
            json={"institution_id": self.inst["id"], "term": "t", "kind": "other",
                  "title": "x"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
