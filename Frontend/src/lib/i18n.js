/**
 * i18n layer (UI spec §123). UI strings live in per-locale catalogs looked up
 * with t(); missing keys fall back to English, so a language is added by adding
 * a catalog, not by rewriting components. Dates and numbers use Intl.
 *
 * A locale is offered in Settings only when its catalog is marked reviewed by a
 * fluent speaker AND covers every English key: machine or partial translations
 * are never shown to users. See Frontend/i18n/README.md for the workflow.
 */
import en from '../../i18n/en.json';
import hi from '../../i18n/hi.json';
import kn from '../../i18n/kn.json';
import ml from '../../i18n/ml.json';
import ta from '../../i18n/ta.json';
import te from '../../i18n/te.json';

const LOCALE_NAMES = {
  en: 'English', te: 'తెలుగు (Telugu)', hi: 'हिन्दी (Hindi)', ta: 'தமிழ் (Tamil)', ml: 'മലയാളം (Malayalam)', kn: 'ಕನ್ನಡ (Kannada)',
};

const catalogs = { en, te, hi, ta, ml, kn };

export const SUPPORTED_LOCALES = Object.keys(LOCALE_NAMES);

export function catalogCoverage(locale) {
  const keys = Object.keys(en.messages);
  const messages = catalogs[locale]?.messages || {};
  const translated = keys.filter((key) => typeof messages[key] === 'string' && messages[key].trim());
  return { translated: translated.length, total: keys.length, reviewed: Boolean(catalogs[locale]?.meta?.reviewed) };
}

/** Locales users may pick: English plus fully translated, reviewed catalogs. */
export function availableLocales() {
  return SUPPORTED_LOCALES.filter((locale) => {
    if (locale === 'en') return true;
    const coverage = catalogCoverage(locale);
    return coverage.reviewed && coverage.translated === coverage.total;
  }).map((locale) => ({ code: locale, name: LOCALE_NAMES[locale] }));
}

function storedLocale() {
  try {
    return localStorage.getItem('grace_locale');
  } catch {
    return null;
  }
}

export function currentLocale() {
  const preferred = storedLocale() || (typeof navigator !== 'undefined' ? navigator.language : 'en') || 'en';
  const base = preferred.toLowerCase().split('-')[0];
  return availableLocales().some((locale) => locale.code === base) ? base : 'en';
}

export function setLocale(locale) {
  try {
    localStorage.setItem('grace_locale', locale);
  } catch {
    // Storage unavailable: the choice lasts for this page only.
  }
  document.documentElement.lang = currentLocale();
}

// Registers (or fully replaces) a locale's catalog. This is a deliberate
// *replace*, not a merge with whatever the locale already holds: now that
// te/hi/ta/ml/kn all ship complete (if unreviewed) translations loaded at
// import time, a merge would make it impossible to deterministically set up
// a "partial" or "empty" catalog for a locale that already has real content
// -- e.g. in tests. No app code calls this today; it exists as the seam for
// loading or swapping a catalog (e.g. once a locale is reviewed) in one
// well-defined step.
export function registerCatalog(locale, messages, meta = { reviewed: false }) {
  catalogs[locale] = { meta, messages: { ...messages } };
}

export function t(key, locale = currentLocale()) {
  return catalogs[locale]?.messages?.[key] ?? en.messages[key] ?? key;
}

export function formatDate(value, options = { day: 'numeric', month: 'short', year: 'numeric' }, locale = currentLocale()) {
  try {
    return new Intl.DateTimeFormat(locale, options).format(new Date(value));
  } catch {
    return '';
  }
}
