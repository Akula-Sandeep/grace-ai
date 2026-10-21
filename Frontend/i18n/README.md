# Grace UI translations

`en.json` is the source catalog. Each other file (`te`, `hi`, `ta`, `ml`, `kn`) holds
translations of the same keys.

A language is offered in **Settings → Appearance → Language** only when:

1. every key in `en.json` has a non-empty translation, and
2. `meta.reviewed` is `true` — set only after a fluent speaker has reviewed the text
   in context (machine translation alone is not enough).

Until then untranslated keys fall back to English and the language stays hidden.

Workflow:

- `npm run i18n:coverage` prints per-language coverage and missing keys.
- Give translators `en.json`; they return the same keys translated.
- After review, set `"reviewed": true` and run `npm run i18n:coverage` again.

Keep placeholders, punctuation style and product names ("Grace") intact.
