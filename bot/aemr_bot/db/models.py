from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
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
    # Житель поделился геолокацией на шаге AWAITING_LOCALITY — бот
    # определил поселение и адрес через services/geo.py и просит
    # подтверждения. dialog_data сохраняет: detected_locality,
    # detected_street, detected_house_number, detected_lat, detected_lon.
    AWAITING_GEO_CONFIRM = "awaiting_geo_confirm"


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
    # UC создаётся миграцией 0001 (Column unique=True). Дублируем в модели,
    # иначе alembic check видит drift (миграция → БД имеет UC, модель → нет).
    #
    # Partial-индексы из миграции 0009 декларируем здесь, чтобы alembic
    # autogenerate не пытался их «удалить» при сравнении модели и БД.
    # postgresql_where + postgresql_using фиксируют partial-условие;
    # SQLAlchemy транслирует это в `WHERE <expr>` при создании индекса.
    __table_args__ = (
        UniqueConstraint("max_user_id", name="users_max_user_id_key"),
        Index(
            "ix_users_pending_pdn_retention",
            "consent_revoked_at",
            postgresql_where=text("consent_revoked_at IS NOT NULL"),
        ),
        Index(
            "ix_users_subscribed_active",
            "subscribed_broadcast",
            "is_blocked",
            postgresql_where=text("subscribed_broadcast = true"),
        ),
        Index(
            "ix_users_stuck_in_funnel",
            "dialog_state",
            "updated_at",
            postgresql_where=text("is_blocked = false"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    # Нормализованный телефон только из цифр, синхронизируется с `phone`
    # через services/users.py::_normalize_phone. Индекс нужен для поиска
    # `/erase phone=`, чтобы он работал и за пределами пары сотен жителей.
    phone_normalized: Mapped[str | None] = mapped_column(String(32), index=True)
    consent_pdn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда житель явно отозвал согласие через сценарий «Уйти из бота».
    # Используется как точка отсечения: по обращениям, принятым до отзыва,
    # оператор может дать финальный ответ через бот; новые обращения после
    # отзыва не принимаются без нового согласия.
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
    # «Нельзя связываться» — гард, разный в разных местах:
    # - broadcasts._eligible_filter — SQL-уровень: subscribed_broadcast +
    #   consent_broadcast_at + NOT is_blocked + first_name != 'Удалено';
    # - operator_reply._deliver_operator_reply — Python-уровень с
    #   исключением для «прощального ответа» по обращениям ДО revoke.
    # Объединяющего @property специально нет: семантика отличается, и
    # унификация даст ложную уверенность «один canonical-гард».


class Operator(Base):
    __tablename__ = "operators"
    __table_args__ = (
        UniqueConstraint("max_user_id", name="operators_max_user_id_key"),
    )

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
    # Композитный индекс (appeal_id, created_at) под горячий паттерн
    # relationship `Appeal.messages`: selectinload фильтрует по
    # appeal_id и сортирует `order_by="Message.created_at"`. Отдельный
    # индекс на appeal_id (index=True ниже) покрывает фильтр, но не
    # сортировку — на длинной переписке Postgres делает Sort-шаг.
    # Композитный закрывает и фильтр, и порядок одним index scan.
    # Создан миграцией 0012.
    __table_args__ = (
        Index("ix_messages_appeal_created", "appeal_id", "created_at"),
    )

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
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="events_idempotency_key_key"),
    )

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


class WizardState(Base):
    """Persistence для wizard state'а оператора (миграция 0011).

    Закрывает проблему «оператор посреди регистрации/рассылки потерял
    state на рестарте бота». In-memory dict'ы в services/wizard_registry
    остаются primary cache (быстро); эта таблица — durability layer.

    На старте бота `wizard_persist.hydrate_into_registry()` загружает
    активные (не expired) записи в in-memory. На каждый set/clear
    handler параллельно зовёт `await wizard_persist.save/delete`.
    """

    __tablename__ = "wizard_state"
    __table_args__ = (
        UniqueConstraint(
            "kind", "operator_max_user_id",
            name="uq_wizard_state_kind_operator",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    operator_max_user_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
