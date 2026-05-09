from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DialogState(StrEnum):
    IDLE = "idle"
    AWAITING_CONSENT = "awaiting_consent"
    AWAITING_CONTACT = "awaiting_contact"
    AWAITING_NAME = "awaiting_name"
    AWAITING_LOCALITY = "awaiting_locality"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_TOPIC = "awaiting_topic"
    AWAITING_SUMMARY = "awaiting_summary"
    # Житель явно нажал «📎 Дополнить» в карточке обращения — ждём от
    # него текст и/или вложения, потом пришиваем к выбранному
    # обращению. dialog_data['appeal_id'] — id целевого обращения.
    AWAITING_FOLLOWUP_TEXT = "awaiting_followup_text"


# Sentinel max_user_id для технической записи anonymous user. После
# полного удаления (erase_pdn) обращения жителя переподвешиваются на
# эту запись через UPDATE appeals.user_id, исходная запись физически
# удаляется. -1 выбран как значение, которое не может встретиться в
# MAX (там user_id положительные BigInt). См. миграцию 0007.
ANONYMOUS_MAX_USER_ID = -1


class AppealStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"
    CLOSED = "closed"


class OperatorRole(StrEnum):
    COORDINATOR = "coordinator"
    AEMR = "aemr"
    EGP = "egp"
    IT = "it"


class MessageDirection(StrEnum):
    FROM_USER = "from_user"
    FROM_OPERATOR = "from_operator"
    SYSTEM = "system"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    # Нормализованный телефон только из цифр, синхронизируется с `phone`
    # через services/users.py::_normalize_phone. Индекс нужен для поиска
    # `/erase phone=`, чтобы он работал и за пределами пары сотен жителей.
    phone_normalized: Mapped[str | None] = mapped_column(String(32), index=True)
    consent_pdn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда житель явно отозвал согласие через кнопку «Отозвать согласие»
    # или через /forget. Используется как точка отсечения «до отзыва /
    # после отзыва»: открытые на момент отзыва обращения остаются в работе
    # и ответ оператора по ним доставляется (право жителя на ответ по
    # 59-ФЗ не зависит от 152-ФЗ); новые обращения после отзыва — нет,
    # нужно дать согласие заново.
    consent_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Отдельное согласие на рассылку. Подписка не требует полного
    # согласия на ПДн (имя/телефон) — для отправки broadcast нужен
    # только max_user_id. См. миграцию 0007 и services/broadcasts.
    consent_broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    subscribed_broadcast: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    dialog_state: Mapped[str] = mapped_column(String(32), default=DialogState.IDLE.value, server_default=DialogState.IDLE.value)
    dialog_data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    appeals: Mapped[list["Appeal"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def contact_forbidden(self) -> bool:
        """Канонический «нельзя связываться» — три маркера в одном.

        Используется в гардах доставки (operator_reply, broadcasts).
        Любой из трёх — отказ:
        - is_blocked: IT-блокировка за злоупотребления;
        - consent_pdn_at IS NULL: согласие не активно (отзыв или новый);
        - first_name == 'Удалено': данные обезличены (после erase_pdn).

        У anonymous-user тоже True (он создаётся с is_blocked=true и
        first_name='Удалено') — на него никогда не должно ничего
        отправляться, на него только переподвешиваются обращения после
        удаления.
        """
        return (
            self.is_blocked
            or self.consent_pdn_at is None
            or self.first_name == "Удалено"
        )


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Appeal(Base):
    __tablename__ = "appeals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=AppealStatus.NEW.value, server_default=AppealStatus.NEW.value, index=True)
    locality: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(500))
    topic: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    admin_message_id: Mapped[str | None] = mapped_column(String(64))
    assigned_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # True, если обращение закрыто из-за отзыва согласия или удаления
    # данных. Используется чтобы не показывать оператору кнопку
    # «🔁 Возобновить» — гард доставки всё равно откажет.
    closed_due_to_revoke: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    user: Mapped[User] = relationship(back_populates="appeals")
    messages: Mapped[list["Message"]] = relationship(back_populates="appeal", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    max_message_id: Mapped[str | None] = mapped_column(String(64))
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    appeal: Mapped[Appeal] = relationship(back_populates="messages")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    update_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_max_user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    SENDING = "sending"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text)
    subscriber_count_at_start: Mapped[int]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default=BroadcastStatus.DRAFT.value, server_default=BroadcastStatus.DRAFT.value, index=True
    )
    delivered_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    admin_message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    deliveries: Mapped[list["BroadcastDelivery"]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    broadcast: Mapped[Broadcast] = relationship(back_populates="deliveries")
