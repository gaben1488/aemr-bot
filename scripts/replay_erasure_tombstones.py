#!/usr/bin/env python3
"""Replay independent deletion/withdrawal ledger against an explicit offline DB."""
import argparse
import asyncio
import os


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database-url",
                        help="Explicit quarantined restored database URL; never printed")
    target.add_argument("--database-env", help="Explicit environment variable containing the offline database URL")
    parser.add_argument("--confirm-offline", action="store_true", required=True,
                        help="Confirm polling, broadcasts, exports and backups are stopped")
    args = parser.parse_args(argv)
    if args.database_env:
        args.database_url = os.environ.get(args.database_env)
        if not args.database_url:
            parser.error("The explicitly selected database environment variable is empty")
    return args


async def replay(database_url):
    # Configure before importing settings/session; an ambient production URL
    # must never select the target of this destructive offline maintenance.
    os.environ["DATABASE_URL"] = database_url
    from aemr_bot.db.session import session_scope
    from aemr_bot.services.data_erasure import replay_restore_tombstones
    async with session_scope() as session:
        count = await replay_restore_tombstones(session)
    print(f"Applied erasure instructions to {count} restored residents; validate before opening traffic.")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(replay(args.database_url))
