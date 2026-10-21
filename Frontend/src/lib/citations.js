import { safeExternalUrl } from './api';

/** Turn "[1]" in an answer into a link to its first source (and so on), with the source as the link title. */
export function linkCitations(content, sources) {
  if (!sources?.length) return content;
  return String(content || '').replace(/\[(\d{1,2})\](?!\()/g, (match, number) => {
    const source = sources[Number(number) - 1];
    const href = source && safeExternalUrl(source.url);
    if (!href) return match;
    const title = String(source.title || source.domain || 'Source').replace(/["\\]/g, '');
    return `[\\[${number}\\]](${href} "Source ${number}: ${title}")`;
  });
}
