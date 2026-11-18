from __future__ import annotations

import argparse
import json

from .billing import (
    adjustments,
    close_bill,
    monthly_summary,
    resolve_conflict,
    review_rows,
)
from .derive import apply_events, conflicts, facts
from .ingest import progress, raw_events, receive
from .publish import publish, read_manifest
from .storage import open_store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地分层数据服务")
    parser.add_argument("database")
    parser.add_argument(
        "action",
        choices=[
            "receive",
            "facts",
            "progress",
            "publish",
            "read",
            "raw-events",
            "conflicts",
            "close-bill",
            "resolve",
            "adjustments",
            "monthly",
            "review",
        ],
    )
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args(argv)
    connection = open_store(args.database)
    try:
        if args.action == "receive":
            data = json.loads(args.payload or "{}")
            events = receive(connection, data["source"], data["batch_id"], data["cursor"], data["records"])
            print(json.dumps({"received": len(events), "changed": apply_events(connection, events)}, ensure_ascii=False))
        elif args.action == "facts":
            print(json.dumps(facts(connection), ensure_ascii=False))
        elif args.action == "progress":
            print(json.dumps(progress(connection), ensure_ascii=False))
        elif args.action == "publish":
            data = json.loads(args.payload or "{}")
            publish(connection, data["name"], data["version"], facts(connection))
            print("ok")
        elif args.action == "read":
            data = json.loads(args.payload or "{}")
            print(json.dumps(read_manifest(connection, data["name"], data["version"]), ensure_ascii=False))
        elif args.action == "raw-events":
            print(json.dumps(raw_events(connection), ensure_ascii=False))
        elif args.action == "conflicts":
            data = json.loads(args.payload) if args.payload else {}
            print(json.dumps(conflicts(connection, data.get("status")), ensure_ascii=False))
        elif args.action == "close-bill":
            data = json.loads(args.payload or "{}")
            close_bill(connection, data["business_date"])
            print("ok")
        elif args.action == "resolve":
            data = json.loads(args.payload or "{}")
            result = resolve_conflict(
                connection,
                data["source"],
                data["business_key"],
                data["business_date"],
                data["candidate_event_id"],
                data["decision"],
                data["reason"],
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.action == "adjustments":
            data = json.loads(args.payload) if args.payload else {}
            print(
                json.dumps(
                    adjustments(connection, data.get("business_date"), data.get("business_key")),
                    ensure_ascii=False,
                )
            )
        elif args.action == "monthly":
            data = json.loads(args.payload or "{}")
            print(json.dumps(monthly_summary(connection, data["month"]), ensure_ascii=False))
        else:  # review
            data = json.loads(args.payload) if args.payload else {}
            print(json.dumps(review_rows(connection, data.get("business_date")), ensure_ascii=False))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
