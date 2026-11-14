# IntelliDocs Contribution Rules

- Keep changes small and scoped to the task.
- Never touch files outside the task's stated scope.
- Run the relevant tests and linters before finishing.
- Never commit secrets or credentials to git.
- Services communicate only through schemas in `/contracts`.
- All organization-specific behavior—languages, document types, departments, and routing rules—must come from `/config`, never hard-coded.
- Use conventional commit messages.
