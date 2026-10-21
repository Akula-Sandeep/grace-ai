import { Link } from 'react-router-dom';
import Icon from './Icon';
import { useAuth } from '../lib/authContext';
import { firstName, greetingFor } from '../lib/firstRun';

const DEFAULT_SUGGESTIONS = [
  { icon: 'clapper', label: 'Tonight’s pick', prompt: 'Find me something great to watch tonight' },
  { icon: 'spark', label: 'Like Interstellar', prompt: 'Recommend a movie like Interstellar, under two hours, not the obvious blockbuster' },
  { icon: 'pin', label: 'Filming locations', prompt: 'Where was Dune: Part Two actually filmed?' },
  { icon: 'film', label: 'Why it looks that way', prompt: 'Why does Blade Runner 2049 look so distinctive?' },
  { icon: 'music', label: 'Soundtracks', prompt: 'What makes the Interstellar soundtrack so memorable?' },
];

function cleanTaste(preferences = []) {
  return preferences
    .find((preference) => preference.polarity === 'prefer' && preference.source !== 'inferred')
    ?.value
    ?.split('')
    .filter((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
    .slice(0, 40)
    .join('')
    .trim();
}

function onboardingDone() {
  try {
    return localStorage.getItem('grace_onboarding_done') === '1';
  } catch {
    return true;
  }
}

export function chatSuggestions(preferences) {
  const taste = cleanTaste(preferences);
  if (!taste) return DEFAULT_SUGGESTIONS;
  return [
    { icon: 'clapper', label: `A ${taste} pick`, prompt: `Recommend a ${taste} movie that fits my taste` },
    { icon: 'heart', label: 'Something relaxed', prompt: `Find a funny ${taste} movie for a relaxed evening` },
    ...DEFAULT_SUGGESTIONS.slice(2),
  ];
}

/** Greeting shown above the composer when a chat is empty. */
export function ChatGreeting({ isAuthenticated, preferences = [] }) {
  const { user } = useAuth();
  const name = isAuthenticated ? firstName(user) : '';
  const heading = isAuthenticated
    ? `${greetingFor()}${name ? `, ${name}` : ''}`
    : 'What are we watching tonight?';
  const askTaste = isAuthenticated && !preferences.some((preference) => preference.source === 'explicit') && !onboardingDone();
  return (
    <div className="vx-home-greeting">
      <span className="vx-home-mark" aria-hidden="true"><Icon name="spark" size={30} strokeWidth={1.45} /></span>
      <h1 className="vx-display">{heading}</h1>
      {askTaste && (
        <Link to="/onboarding" className="vx-home-taste">
          <Icon name="heart" size={14} />
          Tell Grace what you like for better picks
          <Icon name="chevronRight" size={14} />
        </Link>
      )}
    </div>
  );
}

/** Suggestion chips shown under the composer when a chat is empty. */
export function ChatSuggestions({ preferences, disabled, onPrompt }) {
  return (
    <div className="vx-suggestions" aria-label="Suggestions">
      {chatSuggestions(preferences).map((item) => (
        <button key={item.label} type="button" className="vx-suggestion" disabled={disabled} onClick={() => onPrompt(item.prompt)} title={item.prompt}>
          <Icon name={item.icon} size={15} />
          {item.label}
        </button>
      ))}
    </div>
  );
}
