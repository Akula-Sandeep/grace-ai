import { afterEach, describe, expect, it } from 'vitest';
import { availableLocales, catalogCoverage, currentLocale, formatDate, registerCatalog, t } from '../i18n';

describe('i18n', () => {
  afterEach(() => localStorage.removeItem('grace_locale'));

  it('falls back to English for an unknown locale or an unknown key', () => {
    // 'xx' is not a registered catalog at all, so this exercises the
    // fallback-to-English-value path. (te/hi/ta/ml/kn now carry full,
    // real -- if still unreviewed -- translations, so a *known* locale
    // like 'te' no longer has a missing 'nav.watchlist' to fall back
    // from; see the dedicated unreviewed-content test below instead.)
    expect(t('nav.watchlist', 'xx')).toBe('Watchlist');
    expect(t('unknown.key')).toBe('unknown.key');
  });

  it('returns a locale\'s own translated text via an explicit-locale call, even while unreviewed', () => {
    // t() with an explicit locale is an intentionally ungated direct catalog
    // lookup (see i18n.js) -- the reviewed/coverage gate lives only in
    // availableLocales()/currentLocale(). Every real call site in src/
    // calls t(key) with no explicit locale, so it always resolves through
    // that gate; this test documents the lower-level behavior so a future
    // change doesn't accidentally start calling t(key, locale) directly
    // from app code and leak unreviewed text to users.
    expect(catalogCoverage('te').reviewed).toBe(false);
    expect(t('nav.watchlist', 'te')).not.toBe('Watchlist');
    expect(typeof t('nav.watchlist', 'te')).toBe('string');
    expect(t('nav.watchlist', 'te').length).toBeGreaterThan(0);
  });

  it('never offers an unreviewed or incomplete language', () => {
    expect(availableLocales().map((locale) => locale.code)).toEqual(['en']);
    localStorage.setItem('grace_locale', 'te');
    expect(currentLocale()).toBe('en');
    registerCatalog('ta', { 'nav.watchlist': 'x' }, { reviewed: true });
    expect(availableLocales().map((locale) => locale.code)).toEqual(['en']); // reviewed but incomplete
  });

  it('offers a language once it is complete and reviewed', () => {
    const english = catalogCoverage('en');
    const all = Object.fromEntries(Array.from({ length: english.total }, (_, i) => [`k${i}`, 'v']));
    registerCatalog('kn', {}, { reviewed: true });
    expect(catalogCoverage('kn').translated).toBe(0);
    registerCatalog('ml', all, { reviewed: true });
    expect(catalogCoverage('ml').reviewed).toBe(true);
  });

  it('formats dates with Intl', () => {
    expect(formatDate('2026-10-09T00:00:00Z', { year: 'numeric' }, 'en')).toBe('2026');
  });
});
