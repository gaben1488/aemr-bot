# Индексация репозитория aemr-bot

Этот файл — навигационная точка для быстрой индексации проекта в ChatGPT, Claude, Cursor, NotebookLM и других инструментах анализа кода.

Полный индекс кода хранится в корне репозитория как `aemr-bot-index.md`, потому что так его проще скачать, открыть ссылкой или передать в ИИ-инструмент. Файл является производным артефактом и пересоздается автоматически через GitHub Actions.

## Где полный индекс

```text
aemr-bot-index.md
```

## Автоматическое обновление

Workflow `Generate repository index` запускается вручную из GitHub Actions и автоматически при push в `main`, кроме изменений самого `aemr-bot-index.md`. Это исключает бесконечный цикл автокоммитов.

## Обновить индекс вручную локально

Из корня репозитория:

```bash
python scripts/make_repo_index.py --output aemr-bot-index.md --max-file-kb 300
```

После этого файл можно закоммитить обычным способом:

```bash
git add aemr-bot-index.md
git commit -m "Update generated repository index"
git push
```

## Только дерево файлов

Если нужен легкий обзор без содержимого файлов:

```bash
python scripts/make_repo_index.py --output aemr-bot-tree.md --tree-only
```

`aemr-bot-tree.md` остается локальным файлом и не коммитится.

## Что индексируется

Генератор включает небольшие текстовые файлы исходников и конфигурации: Python, Markdown, TOML, YAML, SQL, JSON, Dockerfile, `.gitignore`, `.env.example` и похожие текстовые форматы.

Крупные, бинарные, временные и локальные рабочие файлы пропускаются.
