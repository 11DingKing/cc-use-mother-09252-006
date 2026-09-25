"""测试基座：可变时钟、确定性 ID、每用例独立数据库的 API/服务层基类。"""
from __future__ import annotations

import base64
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from service_09252_006.api import create_app
from service_09252_006.config import Settings
from service_09252_006.db import open_database
from service_09252_006.models import Institution, User

UTC = timezone.utc


class MutableClock:
    """测试时钟：可定点、可推进，始终 aware UTC。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("时钟需要 aware 时间")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


class SequentialIds:
    """确定性 ID 生成器（线程安全，供并发用例共享）。"""

    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def new_id(self) -> str:
        with self._lock:
            self._n += 1
            return f"id-{self._n:06d}"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


class ApiTestCase(unittest.TestCase):
    """每个用例独立数据库与应用实例，时钟可操控。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_url = f"sqlite:///{self._tmp.name}/qev-test.db"
        self.clock = MutableClock(datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC))
        self.app = create_app(Settings(database_url=self.db_url),
                              clock=self.clock, id_generator=SequentialIds())
        self.client = TestClient(self.app)
        self.officer = self._bootstrap_officer()

    def tearDown(self):
        self.client.close()
        self.app.state.database.dispose()
        self._tmp.cleanup()

    def _bootstrap_officer(self) -> dict:
        resp = self.client.post("/users", json={"name": "质量官", "role": "quality_officer"})
        assert resp.status_code == 201, resp.text
        return {"X-Actor-Id": resp.json()["id"]}

    # ---------------------------------------------------------- 便捷方法

    def headers(self, user_id: str) -> dict:
        return {"X-Actor-Id": user_id}

    def create_institution(self, code: str = "SH-01", tz: str = "Asia/Shanghai") -> dict:
        resp = self.client.post(
            "/institutions", json={"code": code, "name": code, "timezone": tz},
            headers=self.officer)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def create_user(self, name: str, role: str, institution_id: str | None = None) -> dict:
        payload = {"name": name, "role": role}
        if institution_id:
            payload["institution_id"] = institution_id
        resp = self.client.post("/users", json=payload, headers=self.officer)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def create_item(self, actor: dict, institution_id: str, *, term: str = "2026-spring",
                    kind: str = "syllabus", title: str = "课程大纲",
                    sensitivity: str = "public") -> dict:
        resp = self.client.post(
            "/evidence/items",
            json={"institution_id": institution_id, "term": term, "kind": kind,
                  "title": title, "sensitivity": sensitivity},
            headers=actor)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def upload(self, actor: dict, item_id: str, text: str) -> dict:
        resp = self.client.post(f"/evidence/items/{item_id}/versions",
                                json={"content_base64": b64(text)}, headers=actor)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def create_package(self, institution_id: str, *, term: str = "2026-spring",
                       deadline: str = "2026-07-01T00:00:00+08:00") -> dict:
        resp = self.client.post(
            "/packages",
            json={"institution_id": institution_id, "term": term,
                  "deadline_at": deadline},
            headers=self.officer)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def pin(self, package_id: str, version_id: str) -> dict:
        resp = self.client.post(f"/packages/{package_id}/items",
                                json={"evidence_version_id": version_id},
                                headers=self.officer)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def seal(self, package_id: str) -> dict:
        resp = self.client.post(f"/packages/{package_id}/seal", headers=self.officer)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def assign(self, package_id: str, reviewer_id: str) -> dict:
        resp = self.client.post(f"/packages/{package_id}/assignments",
                                json={"reviewer_id": reviewer_id}, headers=self.officer)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def review(self, actor: dict, package_id: str, recommendation: str = "approve",
               comments: str = "ok") -> dict:
        resp = self.client.post(f"/packages/{package_id}/reviews",
                                json={"recommendation": recommendation,
                                      "comments": comments},
                                headers=actor)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def decide(self, package_id: str, outcome: str = "approved",
               rationale: str = "通过") -> dict:
        resp = self.client.post(f"/packages/{package_id}/decision",
                                json={"outcome": outcome, "rationale": rationale},
                                headers=self.officer)
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def get_package(self, actor: dict, package_id: str) -> dict:
        resp = self.client.get(f"/packages/{package_id}", headers=actor)
        assert resp.status_code == 200, resp.text
        return resp.json()


class ServiceTestCase(unittest.TestCase):
    """服务层基座：直接操作会话，供并发用例使用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = open_database(f"sqlite:///{self._tmp.name}/svc.db")
        self.db.create_schema()
        self.clock = MutableClock(datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC))
        self.ids = SequentialIds()
        with self.db.write_factory() as session:
            self.officer = User(id="officer-1", name="质量官", role="quality_officer",
                                institution_id=None, active=True,
                                created_at=self.clock.now())
            self.inst = Institution(id="inst-1", code="SH-01", name="上海校",
                                    timezone="Asia/Shanghai",
                                    created_at=self.clock.now())
            self.coord = User(id="coord-1", name="协调员", role="coordinator",
                              institution_id="inst-1", active=True,
                              created_at=self.clock.now())
            session.add_all([self.officer, self.inst, self.coord])
            session.commit()

    def tearDown(self):
        self.db.dispose()
        self._tmp.cleanup()

    def run_concurrently(self, fn, workers: int = 8) -> list:
        """并发执行 fn(index)，返回按序号排列的结果或异常。"""
        results: list = [None] * workers

        def _run(i: int) -> None:
            try:
                results[i] = ("ok", fn(i))
            except Exception as exc:  # noqa: BLE001 - 测试需要收集全部异常
                results[i] = ("err", exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        return results
