import argparse
import signal
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService
from src.domain import Actor


def main(argv=None):
    parser = argparse.ArgumentParser(description="大型场馆人群安全与现场指挥")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8340)
    parser.add_argument("--backfill", action="store_true", help="升级旧库时按已回传批次回填区域在场人数")
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    rules = RuleEngine()
    service = DomainService(repository, rules)
    if args.backfill:
        updated = service.backfill_occupancy(Actor("system", "admin"))
        print("backfilled occupancy for %d zone(s)" % len(updated), flush=True)
    server = create_server(
        args.host,
        args.port,
        service,
        rules,
        str(Path(__file__).resolve().parent / "static"),
    )

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("venue crowd safety service listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
