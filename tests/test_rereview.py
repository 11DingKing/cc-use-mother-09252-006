"""复审流程与并发：后补文件触发复审请求，并发复审只产生一个新一轮包。"""
import unittest
from datetime import datetime, timezone

from helpers import ApiTestCase, ServiceTestCase
from service_09252_006.models import (EvidenceVersion, Package,
                                      ReReviewRequest, User)
from service_09252_006.services import evidence as evidence_svc
from service_09252_006.services import packages as package_svc
from service_09252_006.services import reviews as review_svc
from sqlalchemy import func, select


class ReReviewFlowTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.inst = self.create_institution()
        self.coord = self.create_user("协调员", "coordinator", self.inst["id"])
        self.coord_h = self.headers(self.coord["id"])
        self.rev = self.create_user("评审", "reviewer")
        self.rev_h = self.headers(self.rev["id"])
        self.item = self.create_item(self.coord_h, self.inst["id"])
        self.v1 = self.upload(self.coord_h, self.item["id"], "大纲 v1")["version"]
        self.pkg = self.create_package(self.inst["id"])
        self.pin(self.pkg["id"], self.v1["id"])
        self.seal(self.pkg["id"])
        self.assign(self.pkg["id"], self.rev["id"])
        self.review(self.rev_h, self.pkg["id"])
        self.decide(self.pkg["id"])

    def test_late_evidence_flows_into_next_cycle(self):
        late = self.upload(self.coord_h, self.item["id"], "大纲 v2 后补")
        self.assertIsNotNone(late["rereview_request_id"])

        resp = self.client.post(f"/packages/{self.pkg['id']}/rereview",
                                json={"new_deadline_at": "2026-12-01T00:00:00+08:00"},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 201)
        new_pkg = resp.json()
        self.assertEqual(new_pkg["cycle"], 2)
        self.assertEqual(new_pkg["supersedes_package_id"], self.pkg["id"])
        # 新包预置了最新有效版本（含后补文件）
        self.assertEqual(len(new_pkg["items"]), 1)
        self.assertEqual(new_pkg["items"][0]["version_id"], late["version"]["id"])

        # 复审请求被吸收，旧包结论不受影响
        reqs = self.client.get(f"/packages/{self.pkg['id']}/rereview-requests",
                               headers=self.officer).json()["requests"]
        self.assertEqual(reqs[0]["status"], "absorbed")
        old = self.get_package(self.officer, self.pkg["id"])
        self.assertEqual(old["status"], "decided")
        self.assertEqual(old["decision"]["outcome"], "approved")

    def test_open_rereview_without_trigger_rejected(self):
        other_item = self.create_item(self.coord_h, self.inst["id"],
                                      term="2026-autumn", title="秋季大纲")
        v = self.upload(self.coord_h, other_item["id"], "内容")["version"]
        pkg = self.create_package(self.inst["id"], term="2026-autumn",
                                  deadline="2026-12-01T00:00:00+08:00")
        self.pin(pkg["id"], v["id"])
        self.seal(pkg["id"])
        self.assign(pkg["id"], self.rev["id"])
        self.review(self.rev_h, pkg["id"])
        self.decide(pkg["id"])
        resp = self.client.post(f"/packages/{pkg['id']}/rereview",
                                json={"new_deadline_at": "2027-01-01T00:00:00Z"},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 409)

    def test_upheld_objection_enables_rereview(self):
        obj = self.client.post(f"/packages/{self.pkg['id']}/objections",
                               json={"reason": "评审遗漏企业反馈"}, headers=self.coord_h)
        self.client.post(f"/objections/{obj.json()['id']}/resolve",
                         json={"outcome": "upheld", "note": "属实"}, headers=self.officer)
        resp = self.client.post(f"/packages/{self.pkg['id']}/rereview",
                                json={"new_deadline_at": "2026-12-01T00:00:00+08:00"},
                                headers=self.officer)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["cycle"], 2)


class ConcurrentReReviewTests(ServiceTestCase):
    """服务层并发：真实多线程写同一数据库文件。"""

    def _build_decided_package_with_late_version(self):
        """构造：cycle1 已签发，随后到达一个后补版本（产生复审请求）。"""
        with self.db.write_factory() as s:
            item, _ = evidence_svc.create_item(
                s, self.clock, self.ids, self.coord, institution_id="inst-1",
                term="2026-spring", kind="syllabus", title="大纲", sensitivity="public")
            r = evidence_svc.ingest_version(s, self.clock, self.ids, self.coord,
                                            item_id=item.id, content=b"v1")
            pkg, _ = package_svc.create_package(
                s, self.clock, self.ids, self.officer, institution_id="inst-1",
                term="2026-spring",
                deadline_at=self.clock.now().replace(year=2026, month=6, day=1))
            package_svc.add_item(s, self.clock, self.officer, package_id=pkg.id,
                                 evidence_version_id=r.version.id)
            package_svc.seal_package(s, self.clock, self.officer, package_id=pkg.id)
            reviewer = User(id="rev-1", name="评审", role="reviewer", institution_id=None,
                            active=True, created_at=self.clock.now())
            s.add(reviewer)
            s.flush()
            review_svc.assign_reviewer(s, self.clock, self.ids, self.officer,
                                       package_id=pkg.id, reviewer_id="rev-1")
            review_svc.submit_review(s, self.clock, self.ids, reviewer,
                                     package_id=pkg.id, recommendation="approve",
                                     comments="ok")
            review_svc.issue_decision(s, self.clock, self.ids, self.officer,
                                      package_id=pkg.id, outcome="approved",
                                      rationale="通过")
            late = evidence_svc.ingest_version(s, self.clock, self.ids, self.coord,
                                               item_id=item.id, content=b"v2 late")
            s.commit()
            return pkg.id, late.rereview_request_id

    def test_concurrent_open_rereview_single_winner(self):
        pkg_id, req_id = self._build_decided_package_with_late_version()
        deadline = datetime(2026, 12, 1, tzinfo=timezone.utc)

        def attempt(_i):
            with self.db.write_factory() as s:
                pkg, created = package_svc.open_rereview(
                    s, self.clock, self.ids, self.officer, package_id=pkg_id,
                    new_deadline_at=deadline)
                s.commit()
                return pkg.id, pkg.cycle, created

        results = self.run_concurrently(attempt, workers=8)
        errors = [r for tag, r in results if tag == "err" and isinstance(r, BaseException)]
        self.assertEqual(errors, [], f"并发复审出现异常: {errors}")
        values = [r for tag, r in results if tag == "ok"]
        self.assertEqual(len(values), 8)
        package_ids = {v[0] for v in values}
        self.assertEqual(len(package_ids), 1, "并发复审必须收敛到同一个新包")
        self.assertEqual(sum(1 for v in values if v[2]), 1, "只有一个调用是新创建")

        with self.db.read_factory() as s:
            cycles = s.scalars(
                select(Package).where(Package.institution_id == "inst-1",
                                      Package.term == "2026-spring",
                                      Package.cycle == 2)).all()
            self.assertEqual(len(cycles), 1)
            req = s.get(ReReviewRequest, req_id)
            self.assertEqual(req.status, "absorbed")
            self.assertEqual(req.absorbed_by_package_id, cycles[0].id)

    def test_concurrent_ingest_same_content_single_version(self):
        with self.db.write_factory() as s:
            item, _ = evidence_svc.create_item(
                s, self.clock, self.ids, self.coord, institution_id="inst-1",
                term="2026-spring", kind="other", title="并发材料", sensitivity="public")
            s.commit()
            item_id = item.id

        def attempt(_i):
            with self.db.write_factory() as s:
                result = evidence_svc.ingest_version(
                    s, self.clock, self.ids, self.coord, item_id=item_id,
                    content=b"identical bytes")
                s.commit()
                return result.version.id, result.created

        results = self.run_concurrently(attempt, workers=8)
        values = [r for tag, r in results if tag == "ok"]
        self.assertEqual(len(values), 8)
        self.assertEqual(len({v[0] for v in values}), 1)
        self.assertEqual(sum(1 for v in values if v[1]), 1)
        with self.db.read_factory() as s:
            count = s.scalar(select(func.count(EvidenceVersion.id))
                             .where(EvidenceVersion.item_id == item_id))
            self.assertEqual(count, 1)

    def test_concurrent_seal_and_decision_are_idempotent(self):
        with self.db.write_factory() as s:
            item, _ = evidence_svc.create_item(
                s, self.clock, self.ids, self.coord, institution_id="inst-1",
                term="2026-spring", kind="other", title="并发封存", sensitivity="public")
            r = evidence_svc.ingest_version(s, self.clock, self.ids, self.coord,
                                            item_id=item.id, content=b"v1")
            pkg, _ = package_svc.create_package(
                s, self.clock, self.ids, self.officer, institution_id="inst-1",
                term="2026-spring", deadline_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
            package_svc.add_item(s, self.clock, self.officer, package_id=pkg.id,
                                 evidence_version_id=r.version.id)
            s.commit()
            pkg_id = pkg.id

        def seal_attempt(_i):
            with self.db.write_factory() as s:
                pkg = package_svc.seal_package(s, self.clock, self.officer,
                                               package_id=pkg_id)
                s.commit()
                return pkg.manifest_hash

        results = self.run_concurrently(seal_attempt, workers=6)
        manifests = [r for tag, r in results if tag == "ok"]
        self.assertEqual(len(manifests), 6)
        self.assertEqual(len(set(manifests)), 1, "并发封存必须得到同一清单指纹")

        # 指派+评审后并发签发结论
        with self.db.write_factory() as s:
            reviewer = User(id="rev-1", name="评审", role="reviewer", institution_id=None,
                            active=True, created_at=self.clock.now())
            s.add(reviewer)
            s.flush()
            review_svc.assign_reviewer(s, self.clock, self.ids, self.officer,
                                       package_id=pkg_id, reviewer_id="rev-1")
            review_svc.submit_review(s, self.clock, self.ids, reviewer,
                                     package_id=pkg_id, recommendation="approve",
                                     comments="ok")
            s.commit()

        def decide_attempt(i):
            with self.db.write_factory() as s:
                outcome = "approved" if i % 2 == 0 else "conditional"
                decision = review_svc.issue_decision(
                    s, self.clock, self.ids, self.officer, package_id=pkg_id,
                    outcome=outcome, rationale="并发签发")
                s.commit()
                return decision.id

        results = self.run_concurrently(decide_attempt, workers=6)
        from service_09252_006.errors import ConflictError
        conflicts = [r for tag, r in results if tag == "err" and isinstance(r, ConflictError)]
        oks = [r for tag, r in results if tag == "ok"]
        self.assertTrue(conflicts, "不同内容的并发签发必须产生冲突")
        with self.db.read_factory() as s:
            from service_09252_006.models import Decision
            decisions = s.scalars(select(Decision).where(Decision.package_id == pkg_id)).all()
            self.assertEqual(len(decisions), 1, "并发签发只能留下一个结论")
            for ok_id in oks:
                self.assertEqual(ok_id, decisions[0].id)


if __name__ == "__main__":
    unittest.main()
