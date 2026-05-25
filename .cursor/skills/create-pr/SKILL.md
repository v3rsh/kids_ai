---
name: create-pr
description: Create GitHub pull requests with gh CLI. Use when the user asks to create, open, or submit a PR.
---
# Create Pull Request

## Когда применять

Только по явной просьбе («создай PR», «открой pull request»).

## Workflow

1. Параллельно: `git status`, `git diff`, remote tracking, `git log`, `git diff [base]...HEAD`
2. Проанализировать **все** commits в ветке, не только последний
3. Push при необходимости: `git push -u origin HEAD`
4. Создать PR:

```bash
gh pr create --title "краткий заголовок" --body "$(cat <<'EOF'
## Summary
- ...

## Test plan
- [ ] ...

EOF
)"
```

5. Вернуть URL PR пользователю.

## Правила

- NEVER update git config
- Использовать `gh` для всех GitHub-операций
- Не push без явного запроса (кроме шага создания PR, если ветка не на remote)
- Title и body — на русском, complete sentences
