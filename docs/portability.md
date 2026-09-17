# Portability Checklist

Cockpit is a template, not a host installer.

Before adapting it to another machine or organisation:

- replace user-specific paths with `$HOME` or explicit environment variables;
- choose the applicable agent clients and event names;
- verify the client JSON schema before enabling a hook;
- merge configuration fragments with existing settings;
- keep local state outside the repository;
- remove optional guards that do not address a demonstrated local failure;
- run the isolated test suite and a harmless stdin smoke test; and
- review the resulting diff for names, URLs, tokens, account identifiers,
  customer data, and private paths.

The base implementation assumes Python 3.10+ and SQLite with FTS5. It does
not require third-party packages.
