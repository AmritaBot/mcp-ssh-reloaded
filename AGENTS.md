# AGENTS.md

Repository-wide rules for automated contributors. They override personal style
preferences; when in doubt, follow this file.

## Comments

- English only.
- One line each. Two or more consecutive `#` lines count as a multi-line comment
  and are not allowed.
- No section dividers. Banners such as `# ===== Foo =====`, `# --- Bar ---` or
  `#  Foo  ` are not allowed; use a module or a function to separate concerns.
- Prefer deleting. If the name, type or value already says it, drop the comment.
  A comment must carry information the code cannot.
- Docstrings: one short line. Do not restate the signature.

## Typing

- No `# type: ignore` and no `# pyright: ignore`. Fix the cause: widen the
  annotation, add a guard, or narrow the type at the boundary.

## Tests

- No fake tests. `assert True`, empty bodies and assertions that cannot fail are
  not tests. Every test must fail when the behaviour it covers is broken.
- `pytest.mark.skipif` is fine when it guards a real environment requirement
  (a live SSH target, a specific OS). It is never a way to silence a failure.

## Commits and pull requests

- English only.
- Conventional prefix: `fix:`, `feat:`, `chore:`, `style:`, `docs:`, `test:`.
- The body explains why, not what. Keep it short.

## Baseline

- `ruff check` passes.
- `pyright` reports 0 errors.
- `pytest` passes.
- Bump `version` in `pyproject.toml` and run `uv lock` in every PR.
