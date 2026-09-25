"""离线完整性核验：干净库通过；任何篡改（内容/清单/结论/审计链）都被发现。"""
import sqlite3
import unittest

from helpers import ApiTestCase
from service_09252_006.cli import main as cli_main


class IntegrityCliTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = self.db_url.removeprefix("sqlite:///")
        self._build_decided_flow()

    def _build_decided_flow(self):
        inst = self.create_institution()
        coord = self.create_user("协调员", "coordinator", inst["id"])
        coord_h = self.headers(coord["id"])
        rev = self.create_user("评审", "reviewer")
        rev_h = self.headers(rev["id"])
        item = self.create_item(coord_h, inst["id"])
        v1 = self.upload(coord_h, item["id"], "大纲 v1")["version"]
        self.upload(coord_h, item["id"], "大纲 v2")  # 形成版本链
        pkg = self.create_package(inst["id"])
        self.pin(pkg["id"], v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], rev["id"])
        self.review(rev_h, pkg["id"])
        self.decide(pkg["id"])

    def _verify_rc(self) -> int:
        return cli_main(["verify", "--database-url", self.db_url])

    def _tamper(self, sql: str) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(sql)
        con.commit()
        con.close()

    def test_clean_database_verifies(self):
        self.assertEqual(self._verify_rc(), 0)

    def test_detects_content_tamper(self):
        self._tamper("UPDATE evidence_versions SET content = x'00' WHERE seq = 1")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_recorded_fingerprint_tamper(self):
        self._tamper("UPDATE evidence_versions SET sha256 = 'f' || substr(sha256, 2)")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_version_chain_tamper(self):
        self._tamper("UPDATE evidence_versions SET supersedes_id = NULL WHERE seq = 2")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_manifest_tamper(self):
        self._tamper("UPDATE package_items SET sha256 = '0' || substr(sha256, 2)")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_decision_tamper(self):
        self._tamper("UPDATE decisions SET rationale = '被改写的结论'")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_audit_trail_tamper(self):
        self._tamper("UPDATE audit_events SET payload_json = '{}' WHERE seq = 2")
        self.assertEqual(self._verify_rc(), 1)

    def test_detects_audit_row_deletion(self):
        self._tamper("DELETE FROM audit_events WHERE seq = (SELECT MAX(seq) FROM audit_events)")
        self.assertEqual(self._verify_rc(), 1)


if __name__ == "__main__":
    unittest.main()
