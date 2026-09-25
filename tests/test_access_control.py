"""最小披露与权限变化：敏感企业反馈按机构+角色+评审窗口披露，变更立即生效。"""
import unittest

from helpers import ApiTestCase


class AccessControlTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution(code="SH-01", tz="Asia/Shanghai")
        self.other = self.create_institution(code="NY-02", tz="America/New_York")
        self.admin = self.create_user("机构管理员", "institution_admin", self.inst["id"])
        self.admin_h = self.headers(self.admin["id"])
        self.coord = self.create_user("提交人", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.coord2 = self.create_user("同事", "coordinator", self.inst["id"])
        self.coord2_h = self.headers(self.coord2["id"])
        self.other_admin = self.create_user("他校管理员", "institution_admin",
                                            self.other["id"])
        self.other_h = self.headers(self.other_admin["id"])
        self.reviewer = self.create_user("评审", "reviewer")
        self.rev_h = self.headers(self.reviewer["id"])
        # 敏感企业反馈
        self.item = self.create_item(self.coord_h, self.inst["id"],
                                     kind="enterprise_feedback", title="企业反馈",
                                     sensitivity="restricted")
        self.v1 = self.upload(self.coord_h, self.item["id"], "机密：雇主评价")["version"]

    def _content(self, actor: dict, version_id: str | None = None):
        return self.client.get(
            f"/evidence/versions/{version_id or self.v1['id']}/content", headers=actor)

    def _item_view(self, actor: dict):
        return self.client.get(f"/evidence/items/{self.item['id']}", headers=actor)

    def test_restricted_feedback_minimal_disclosure(self):
        self.assertEqual(self._content(self.officer).status_code, 200)      # 质量官
        self.assertEqual(self._content(self.admin_h).status_code, 200)      # 本机构管理员
        self.assertEqual(self._content(self.coord_h).status_code, 200)      # 提交者本人

        denied = self._content(self.coord2_h)                               # 本机构普通协调员
        self.assertEqual(denied.status_code, 403)
        meta = self._item_view(self.coord2_h)                               # 但元数据可见
        self.assertEqual(meta.status_code, 200)
        self.assertFalse(meta.json()["versions"][0]["content_accessible"])

        self.assertEqual(self._content(self.other_h).status_code, 404)      # 跨机构不可见
        self.assertEqual(self._item_view(self.other_h).status_code, 404)
        self.assertEqual(self._content(self.rev_h).status_code, 404)        # 未指派评审员

    def test_reviewer_window_opens_and_closes_with_review(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], self.reviewer["id"])

        self.assertEqual(self._content(self.rev_h).status_code, 200)  # 评审窗口内可见
        self.assertEqual(self._item_view(self.rev_h).status_code, 200)

        self.review(self.rev_h, pkg["id"])
        self.decide(pkg["id"])
        closed = self._content(self.rev_h)                            # 签发后窗口关闭
        self.assertEqual(closed.status_code, 403)

    def test_revoked_assignment_closes_access_immediately(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], self.reviewer["id"])
        self.assertEqual(self._content(self.rev_h).status_code, 200)

        resp = self.client.delete(
            f"/packages/{pkg['id']}/assignments/{self.reviewer['id']}",
            headers=self.officer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._content(self.rev_h).status_code, 404)  # 指派撤销即不可见

    def test_role_change_takes_effect_immediately(self):
        self.assertEqual(self._content(self.admin_h).status_code, 200)
        # 降级为协调员（非提交者）→ 敏感内容立即可见变不可见
        resp = self.client.patch(f"/users/{self.admin['id']}",
                                 json={"role": "coordinator"}, headers=self.officer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._content(self.admin_h).status_code, 403)
        # 恢复管理员 → 立即恢复
        self.client.patch(f"/users/{self.admin['id']}",
                          json={"role": "institution_admin"}, headers=self.officer)
        self.assertEqual(self._content(self.admin_h).status_code, 200)

    def test_reviewer_reassigned_role_loses_reviewer_access(self):
        pkg = self.create_package(self.inst["id"])
        self.pin(pkg["id"], self.v1["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], self.reviewer["id"])
        self.assertEqual(self._content(self.rev_h).status_code, 200)
        # 评审员被调整为外校协调员 → 评审通道与内容访问同时失效
        resp = self.client.patch(
            f"/users/{self.reviewer['id']}",
            json={"role": "coordinator", "institution_id": self.other["id"]},
            headers=self.officer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._content(self.rev_h).status_code, 404)
        submit = self.client.post(f"/packages/{pkg['id']}/reviews",
                                  json={"recommendation": "approve", "comments": ""},
                                  headers=self.rev_h)
        self.assertEqual(submit.status_code, 403)

    def test_deactivated_user_is_unauthenticated(self):
        resp = self.client.patch(f"/users/{self.coord['id']}",
                                 json={"active": False}, headers=self.officer)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._item_view(self.coord_h).status_code, 401)

    def test_public_material_visible_within_institution(self):
        public_item = self.create_item(self.coord_h, self.inst["id"],
                                       kind="syllabus", title="公开大纲")
        v = self.upload(self.coord_h, public_item["id"], "公开内容")["version"]
        self.assertEqual(self._content(self.coord2_h, v["id"]).status_code, 200)
        self.assertEqual(self._content(self.other_h, v["id"]).status_code, 404)


if __name__ == "__main__":
    unittest.main()
