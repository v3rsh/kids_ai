---
name: git-commit
description: Create git commits with Conventional Commits format and safety checks. Use when the user asks to commit, stage, or write a commit message.
---
# Git Commit

## Когда применять

Только по явной просьбе пользователя («сделай commit», «закоммить»). Не коммитить без запроса.

## Безопасность

- NEVER update git config
- NEVER destructive git (force push, hard reset) без явного запроса
- NEVER skip hooks (`--no-verify`) без явного запроса
- NEVER force push to main/master
- NEVER commit `.env` и файлы с секретами — предупредить пользователя
- Avoid `git commit --amend` кроме случаев: пользователь попросил amend; HEAD создан в этом чате; commit не pushed

## Workflow

1. Параллельно: `git status`, `git diff`, `git log -5 --oneline`
2. Проанализировать изменения, выбрать type/scope
3. `git add` релевантные файлы
4. Commit через HEREDOC:

```bash
git commit -m "$(cat <<'EOF'
feat(handlers): краткое описание на русском

EOF
)"
```

5. `git status` — проверить успех. При отказе pre-commit hook — исправить и **новый** commit, не amend.

## Conventional Commits 1.0.0

Формат: `<type>[(scope)][!]: <description>`

| Type | Когда |
|------|-------|
| feat | новая функциональность |
| fix | исправление бага |
| refactor | реструктуризация без изменения поведения |
| docs | только документация |
| test | тесты |
| chore/ci/build | инфра, зависимости, CI |

Правила: description — русский, lowercase, без точки, imperative; type/scope — английский.

Scopes проекта: `handlers`, `services`, `database`, `utils`, `keyboards`, `states`, `config`, `docker`, `moderator`, `admin`, `storage`, `notifications`, `intake`, `jury`, `registry`, `docs`, `foundation`.

Примеры:
```
feat(handlers): добавить ежедневное напоминание
fix(database): исправить утечку соединений при высокой нагрузке
chore: обновить pybotx до 0.76.0
```
