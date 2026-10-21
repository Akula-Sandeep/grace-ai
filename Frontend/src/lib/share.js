/** Sharing helpers (UI spec §137-§138): copy a link, export a conversation as Markdown. */
export async function copyLink(url = window.location.href) {
  if (!navigator.clipboard?.writeText) throw new Error('Copying is unavailable in this browser.');
  await navigator.clipboard.writeText(url);
}

export function conversationMarkdown(messages, title = 'Grace conversation') {
  const lines = [`# ${title}`, '', `_Exported ${new Date().toISOString().slice(0, 10)}_`, ''];
  for (const message of messages || []) {
    if (!message?.content || (message.role !== 'user' && message.role !== 'assistant')) continue;
    lines.push(`## ${message.role === 'user' ? 'You' : 'Grace'}`, '', String(message.content).trim(), '');
    const sources = (message.sources || []).filter((source) => source?.url);
    if (message.role === 'assistant' && sources.length) {
      lines.push('Sources:', ...sources.map((source, index) => `${index + 1}. [${source.title || source.domain}](${source.url})`), '');
    }
  }
  return lines.join('\n');
}

export function downloadText(filename, text, type = 'text/markdown') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
