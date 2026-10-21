/** Every keyboard shortcut, in one place: the "?" sheet and Settings → Help both list these. */
export const SHORTCUTS = [
  { keys: ['Ctrl/⌘', 'K'], label: 'Command palette' },
  { keys: ['Ctrl/⌘', 'Shift', 'O'], label: 'New chat' },
  { keys: ['/'], label: 'Search movies and people' },
  { keys: ['Shift', 'Esc'], label: 'Focus the message box' },
  { keys: ['Ctrl/⌘', '\\'], label: 'Show or hide the sidebar' },
  { keys: ['?'], label: 'Show keyboard shortcuts' },
  { keys: ['Enter'], label: 'Send message', context: 'In the message box' },
  { keys: ['Shift', 'Enter'], label: 'New line', context: 'In the message box' },
  { keys: ['/'], label: 'Commands such as /similar or /watchlist', context: 'At the start of a message' },
];

/** Composer commands: a template to complete ("{}" marks where the cursor goes) or a page to open. */
export const SLASH_COMMANDS = [
  { command: '/similar', label: 'Films like…', template: 'Recommend films like {}' },
  { command: '/where', label: 'Where to watch…', template: 'Where can I watch {}?' },
  { command: '/compare', label: 'Compare two films', template: 'Compare {} and ' },
  { command: '/add', label: 'Add to watchlist', template: 'Add {} to my watchlist' },
  { command: '/remind', label: 'Remind me to watch…', template: 'Remind me to watch {} tonight at 8 pm' },
  { command: '/rate', label: 'Rate a film', template: 'Rate {} 8/10' },
  { command: '/watchlist', label: 'Open your watchlist', route: '/watchlist', signedIn: true },
  { command: '/watched', label: 'Open what you’ve watched', route: '/watched', signedIn: true },
  { command: '/taste', label: 'Open your taste profile', route: '/taste', signedIn: true },
];

/** Commands matching what the user typed ("/re" → /remind), or [] when it isn't a command. */
export function matchSlashCommands(text, { signedIn = false } = {}) {
  const match = /^\/([a-z]*)$/i.exec(text || '');
  if (!match) return [];
  const typed = `/${match[1].toLowerCase()}`;
  return SLASH_COMMANDS.filter((item) => item.command.startsWith(typed) && (signedIn || !item.signedIn));
}

/** The text and cursor position after choosing a template command. */
export function applyTemplate(template) {
  const at = template.indexOf('{}');
  const text = template.replace('{}', '');
  return { text, cursor: at < 0 ? text.length : at };
}
