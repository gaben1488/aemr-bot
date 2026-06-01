"""Создание PR с изменениями настроек (синхронизация с репозиторием).

Выделено из god-объекта `admin_settings.py`. Связная ответственность:
экран подтверждения PR (blockers: GITHUB_PAT / автор коммитов),
создание PR через `services/repo_sync` (mark_synced + audit), проверка
расхождений локальных настроек с main в репо (diff).

`from aemr_bot.services import repo_sync` делается ВНУТРИ функций (как
в исходнике) — тесты патчат атрибуты реального модуля repo_sync, это
устойчиво к порядку тестов и месту функции (урок PR #139). `os`
импортируется на уровне модуля: тесты патчат `<mod>.os.environ`.
"""
from __future__ import annotations

import os

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen


async def _show_pr_confirm(event) -> None:

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")

    pat_present = bool(os.environ.get("GITHUB_PAT", "").strip())
    if not dirty:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Нет несинхронизированных изменений.\n"
                "· · · · · · · ·\n"
                "Все настройки совпадают с последним PR в репо."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    blockers: list[str] = []
    if not pat_present:
        blockers.append("• GITHUB_PAT не задан в .env (см. infra/.env.example)")
    if not name:
        blockers.append("• Не задан автор коммитов — раздел «👤 Автор»")
    if not email:
        blockers.append("• Не задан email автора — раздел «👤 Автор»")

    keys_preview = "\n".join(f"• {k}" for k in dirty[:10])
    if len(dirty) > 10:
        keys_preview += f"\n…и ещё {len(dirty) - 10}"

    if blockers:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "💾 Создать PR с изменениями\n"
                "· · · · · · · ·\n"
                f"Будет включено {len(dirty)} ключей:\n{keys_preview}\n\n"
                "❌ Нельзя создать PR:\n" + "\n".join(blockers) +
                "\n\nИзменения уже применены в боте — это\n"
                "только про их фиксацию в репозитории."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "💾 Создать PR с изменениями\n"
            "· · · · · · · ·\n"
            f"Будет включено {len(dirty)} ключей:\n{keys_preview}\n\n"
            f"Автор: {name} <{email}>\n\n"
            "После создания PR откройте его в браузере,\n"
            "проверьте diff и нажмите Merge. Auto-deploy\n"
            "подхватит изменения в течение 10 минут."
        ),
        attachments=[kbds.op_settings_pr_confirm_keyboard()],
    )


async def _create_pr(event, operator_id: int) -> None:
    from aemr_bot.services import repo_sync

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        runtime_config = await settings_store.export_synced(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")
        op_record = await ops_svc.get(session, operator_id)
    operator_name = op_record.full_name if op_record else f"id={operator_id}"

    cfg_repo = repo_sync.load_config_from_env_and_settings(
        author_name=name, author_email=email,
    )
    if cfg_repo is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "❌ Не настроено GitHub-подключение.\n"
                "· · · · · · · ·\n"
                "Заполните GITHUB_PAT в .env и/или\n"
                "автора коммитов в меню «👤 Автор»."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    if not dirty:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Нет несинхронизированных изменений.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    result = await repo_sync.create_settings_pr(
        cfg_repo,
        runtime_config=runtime_config,
        dirty_keys=dirty,
        operator_name=operator_name,
        operator_id=operator_id,
    )
    if not result.ok:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "❌ Не удалось создать PR.\n"
                "· · · · · · · ·\n"
                f"Причина: {result.reason}\n"
                f"{result.message}"
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    async with session_scope() as session:
        await settings_store.mark_synced(session, dirty)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="settings_pr_created",
            target=cfg_repo.repo,
            details={
                "pr_number": result.pr_number,
                "pr_url": result.pr_url,
                "branch": result.branch,
                "keys": dirty,
            },
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"✅ PR создан: #{result.pr_number}\n"
            f"· · · · · · · ·\n"
            f"Ветка: {result.branch}\n"
            f"Изменено ключей: {len(dirty)}\n\n"
            f"Откройте PR в браузере, проверьте diff\n"
            f"и нажмите Merge.\n\n"
            f"Auto-deploy подхватит изменения в течение\n"
            f"10 минут после мержа."
        ),
        attachments=[kbds.op_settings_pr_done_keyboard(result.pr_url)],
    )


async def _show_pr_diff(event) -> None:
    from aemr_bot.services import repo_sync

    async with session_scope() as session:
        dirty = await settings_store.get_dirty_keys(session)
        local = await settings_store.export_synced(session)
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")

    if not os.environ.get("GITHUB_PAT", "").strip():
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Проверка расхождений с репо\n"
                "· · · · · · · ·\n"
                "GITHUB_PAT не задан в .env.\n\n"
                f"Локально dirty-ключей: {len(dirty)}\n"
                + ("\n".join(f"• {k}" for k in dirty[:10]) if dirty else "—")
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    cfg_repo = repo_sync.load_config_from_env_and_settings(
        author_name=name or "bot", author_email=email or "bot@example.com",
    )
    if cfg_repo is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="❌ Не настроено GitHub-подключение.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    remote, reason = await repo_sync.fetch_main_runtime_config(cfg_repo)
    if remote is None and reason == "not_in_repo":
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "📥 Проверка расхождений с репо\n"
                "· · · · · · · ·\n"
                "Файла seed/runtime_config.json в main\n"
                "пока нет. Первый PR создаст его."
            ),
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    if remote is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=f"❌ Не удалось скачать из репо: {reason}",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return

    diffs: list[str] = []
    for key in settings_store.SYNCED_KEYS:
        local_val = local.get(key)
        remote_val = remote.get(key)
        if local_val != remote_val:
            diffs.append(key)
    if not diffs:
        body = "✅ Локально и в репо всё одинаково."
    else:
        body = (
            f"⚠️ Различаются {len(diffs)} ключей:\n"
            + "\n".join(f"• {k}" for k in diffs)
            + "\n\nЕсли локальные изменения новее — создайте PR.\n"
            + "Если в репо есть изменения, которых нет\n"
            + "локально (например, через ручной PR) —\n"
            + "перезапустите бота, он перечитает seed."
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "📥 Проверка расхождений с репо\n"
            "· · · · · · · ·\n"
            + body
        ),
        attachments=[kbds.op_back_to_settings_keyboard()],
    )
