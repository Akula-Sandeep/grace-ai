// Prints translation coverage per locale; exits 1 if a reviewed catalog is incomplete.
import { readFileSync, readdirSync } from 'node:fs';

const dir = new URL('../i18n/', import.meta.url);
const english = JSON.parse(readFileSync(new URL('en.json', dir), 'utf8'));
const keys = Object.keys(english.messages);
let failed = false;
for (const file of readdirSync(dir).filter((name) => name.endsWith('.json') && name !== 'en.json')) {
  const catalog = JSON.parse(readFileSync(new URL(file, dir), 'utf8'));
  const missing = keys.filter((key) => !catalog.messages?.[key]?.trim());
  const extra = Object.keys(catalog.messages || {}).filter((key) => !keys.includes(key));
  const status = catalog.meta?.reviewed ? 'reviewed' : 'not reviewed';
  console.log(`${file.replace('.json', '')}: ${keys.length - missing.length}/${keys.length} translated, ${status}${extra.length ? `, ${extra.length} unknown keys` : ''}`);
  if (catalog.meta?.reviewed && (missing.length || extra.length)) {
    console.error(`  reviewed catalog is incomplete: missing ${missing.join(', ') || '-'}; unknown ${extra.join(', ') || '-'}`);
    failed = true;
  }
}
process.exit(failed ? 1 : 0);
