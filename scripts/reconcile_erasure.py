#!/usr/bin/env python3
"""IT records manual copy verification; the bot never deletes MAX messages."""
import argparse
import asyncio

from aemr_bot.services import data_erasure


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--initialize-journal", action="store_true", help="Explicit first install on an existing DB; never use for restore")
    group.add_argument("--confirm-copy-absent", type=int, metavar="COPY_ID")
    group.add_argument("--recover-copy-mid", nargs=2, metavar=("COPY_ID", "MESSAGE_ID"))
    group.add_argument("--confirm-backups-clean", nargs=2, metavar=("TOMBSTONE_DIGEST", "ERASED_BEFORE_ISO"))
    args = parser.parse_args()
    if args.initialize_journal:
        await data_erasure.initialize_journal(allow_existing_database=True)
        result = True
    elif args.confirm_copy_absent is not None:
        result = await data_erasure.reconcile_copy(args.confirm_copy_absent, confirmed_absent=True)
    elif args.recover_copy_mid:
        copy_id, mid = args.recover_copy_mid
        result = await data_erasure.reconcile_copy(int(copy_id), recovered_message_id=mid)
    else:
        digest, cutoff = args.confirm_backups_clean
        result = await data_erasure.confirm_backup_cleanup(digest, expected_erased_before=cutoff)
    if not result:
        raise SystemExit("Reconciliation rejected: record is absent or state does not permit this transition")
    print("Reconciliation recorded; retain the manual verification evidence with the deletion act.")


if __name__ == "__main__":
    asyncio.run(main())
