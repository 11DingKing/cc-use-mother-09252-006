"""命令行入口：离线完整性核验与服务启动。

用法：
    python -m service_09252_006.cli verify [--database-url URL]
    python -m service_09252_006.cli serve [--host H] [--port P]
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import Settings, load_settings
from .db import open_database
from .verification import verify_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qev", description="国际课程质量证据链")
    sub = parser.add_subparsers(dest="command", required=True)

    verify_p = sub.add_parser("verify", help="离线核验证据链完整性（不需要服务进程）")
    verify_p.add_argument("--database-url", default=None,
                          help="缺省读取 QEV_DATABASE_URL 或用户数据目录")

    serve_p = sub.add_parser("serve", help="启动 HTTP 服务")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "verify":
        settings = (Settings(database_url=args.database_url)
                    if args.database_url else load_settings())
        database = open_database(settings.database_url)
        database.create_schema()
        try:
            with database.read_factory() as session:
                report = verify_session(session)
        finally:
            database.dispose()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.ok else 1

    if args.command == "serve":
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(settings=load_settings()), host=args.host, port=args.port)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
