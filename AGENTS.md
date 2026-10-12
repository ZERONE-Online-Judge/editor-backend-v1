# Commit message rules

Apply these rules to every commit in this independent Editor project.

- Format: `type(scope): Subject`, then an optional body and footer. Separate
  each present section with exactly one blank line. The scope is optional.
- Use one of `feat`, `fix`, `build`, `chore`, `ci`, `docs`, `style`, `refactor`,
  `test`, or `perf` as the lowercase type.
- Keep the entire header at 50 characters or fewer. Capitalize the first
  letter of an English subject; the required lowercase type is exempt.
- Write the subject as an imperative command, without past tense or a final
  period.
- Keep each body line at 72 characters or fewer. Explain what changed and
  why, rather than implementation steps. Omit the body if the header suffices.
- Use the optional footer for issue references, such as `Resolves: #123`.
- Before committing, verify the proposed message against every rule above.

Example:

```text
fix(operators): Show correct contest role title

Show reviewer roles ahead of general operator roles so the displayed
title matches the permissions selected for the account.
```
