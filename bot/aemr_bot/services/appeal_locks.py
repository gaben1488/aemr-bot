"""Cross-process coordination of reply delivery and appeal status changes."""
from sqlalchemy import text

# PostgreSQL's two-int advisory lock namespace, separate from other services.
_NAMESPACE = 0x41454D52


async def hold(session, appeal_id: int) -> None:
    """Hold until transaction end; external sends must not be retried blindly."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :appeal_id)"),
            {"namespace": _NAMESPACE, "appeal_id": appeal_id},
        )
