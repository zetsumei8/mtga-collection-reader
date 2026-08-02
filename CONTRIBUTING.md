# Contributing

Keep the project focused on reliable collection archival and export. Deck construction, recommendations, and deck publishing are outside its scope.

Before opening a pull request:

1. Keep changes Windows-safe and preserve the manual JSON fallback.
2. Add or update an offline test for changed behavior.
3. Run `python tests/test_v520.py` and `python -m compileall -q .`.
4. Do not commit collection databases, exports, tokens, helper binaries, or credentials.

Changes that touch the Arena helper boundary must retain read-only behavior, loopback-only networking, clear user-visible failures, and a safe fallback when Arena changes.
