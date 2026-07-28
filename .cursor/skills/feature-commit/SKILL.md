---
name: feature-commit
description: >-
  Commits completed feature work with Conventional Commits (feat:/fix:)
  without pushing. Use only when the user explicitly asks to commit, 提交,
  提交代码, or 按 skill 提交 after finishing a feature or fix.
---

# Feature Commit（功能完成提交）

## When to use

**Only when the user explicitly asks** to commit（提交 / commit / 提交代码 / 按 skill 提交）.

Do **not** auto-commit after finishing a feature. Completing work may end with review; wait for an explicit commit request.

## Preconditions

1. There are real changes to commit (`git status` not clean of relevant work).
2. Prefer that post-task code review already ran（or user said 跳过 review）.
3. If review listed **阻断** issues still open, remind the user and ask whether to commit anyway—do not silently commit broken work.

## Do / Don't

| Do | Don't |
|----|--------|
| Local `git commit` only | `git push` unless user explicitly asks |
| One logical feature/fix per commit when practical | Mix unrelated features in one commit |
| Conventional Commits message | Amend / force-push / skip hooks unless user asks |
| Exclude secrets (`.env`, credentials, keys) | Commit temp/diag junk unless user wants it |

Push is **opt-in**（用户说 push / 推上去）—typical flow is many local commits, then one batch push later.

## Workflow

Run in parallel first:

```bash
git status
git diff
git diff --staged
git log -5 --oneline
```

Then:

1. **Scope** — Stage only files belonging to this feature/fix. Leave unrelated dirty files unstaged; if unclear, ask once.
2. **Message** — Draft Conventional Commits (see below). Focus on **why**, 1–2 sentences body optional.
3. **Commit** — Stage + commit. On Windows PowerShell prefer:

   ```powershell
   git add <paths...>
   git commit -m "feat(scope): short summary" -m "Optional body explaining why."
   ```

   If bash/`sh` is available, HEREDOC is also fine:

   ```bash
   git commit -m "$(cat <<'EOF'
   feat(scope): short summary

   Optional body.
   EOF
   )"
   ```

4. **Verify** — `git status` after commit; report hash + subject. Reminder: not pushed.

5. **Push (only if asked)** — `git push` (or `git push -u origin HEAD` if no upstream). Never `--force` to main/master unless user explicitly requests.

## Commit message format

```
<type>(<optional-scope>): <short summary in English or Chinese>

[optional body]
```

**Types** (common):

| type | When |
|------|------|
| `feat` | New user-facing capability or API |
| `fix` | Bug fix |
| `refactor` | Internal change, no behavior intent |
| `docs` | Docs only |
| `chore` | Tooling, config, deps, non-feature chores |
| `test` | Tests only |
| `perf` | Performance |

**Examples:**

```
feat(auth): add JWT login and role claims

fix(chat): prevent empty session_id from creating duplicate threads

chore: ignore local diag scripts
```

Keep summary ≤ ~72 chars; imperative or concise descriptive style matching recent `git log` if the repo already has a pattern.

## Safety (hard rules)

- Never update git config.
- Never commit secrets; warn if user asks to include them.
- Never `--no-verify` / skip hooks unless user asks.
- Never destructive git (`push --force`, hard reset) unless user explicitly asks; warn on force-push to main/master.
- Do not amend unless user asks **and** amend conditions from the user git rules are met.
- No empty commits.

## After commit (reply)

One short block:

- Commit: `<hash>` — `<subject>`
- Files: brief list or count
- Push: not done（or done, if user asked）
