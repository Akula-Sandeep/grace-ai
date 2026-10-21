import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import Icon from './Icon';

/**
 * Search box with type-ahead suggestions (UI spec §21): movies and people as
 * you type, keyboard navigable, Enter on no selection runs the full search.
 */
export default function SearchSuggest({ value, onChange, onSubmit, onPickMovie, onPickPerson, inputRef, placeholder }) {
  const [suggestions, setSuggestions] = useState([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const requestRef = useRef(0);
  const query = value.trim();

  useEffect(() => {
    if (query.length < 2) return undefined;
    const requestId = ++requestRef.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      const [movies, people] = await Promise.allSettled([
        api(`/movies/search?q=${encodeURIComponent(query)}`, { signal: controller.signal }),
        api(`/people/search?q=${encodeURIComponent(query)}`, { signal: controller.signal }),
      ]);
      if (requestId !== requestRef.current) return;
      const items = [
        ...(movies.status === 'fulfilled' ? movies.value.results || [] : []).slice(0, 5).map((movie) => ({
          kind: 'movie', id: movie.id, label: movie.title, detail: String(movie.release_date || '').slice(0, 4),
        })),
        ...(people.status === 'fulfilled' ? people.value.results || [] : []).slice(0, 3).map((person) => ({
          kind: 'person', id: person.id, label: person.name, detail: person.known_for_department || 'Person',
        })),
      ].filter((item) => item.id && item.label);
      setSuggestions(items);
      setActive(-1);
      setOpen(items.length > 0);
    }, 300);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  const visible = open && query.length >= 2 && suggestions.length > 0;

  function pick(item) {
    setOpen(false);
    if (item.kind === 'movie') onPickMovie(item.id);
    else onPickPerson(item.id);
  }

  return (
    <form
      className="search"
      role="search"
      onSubmit={(event) => {
        if (visible && active >= 0) {
          event.preventDefault();
          pick(suggestions[active]);
          return;
        }
        setOpen(false);
        onSubmit(event);
      }}
    >
      <input
        ref={inputRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (!visible) return;
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            setActive((index) => Math.min(suggestions.length - 1, index + 1));
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            setActive((index) => Math.max(-1, index - 1));
          } else if (event.key === 'Escape') {
            setOpen(false);
          }
        }}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        placeholder={placeholder}
        aria-label="Search movies"
        role="combobox"
        aria-expanded={visible}
        aria-controls="search-suggestions"
        aria-autocomplete="list"
        aria-activedescendant={visible && active >= 0 ? `search-suggestion-${active}` : undefined}
        maxLength={200}
      />
      <button type="submit" aria-label="Search movies">
        <Icon name="search" size={17} />
      </button>
      {visible && (
        <ul id="search-suggestions" role="listbox" aria-label="Suggestions" className="search-suggestions">
          {suggestions.map((item, index) => (
            <li
              key={`${item.kind}-${item.id}`}
              id={`search-suggestion-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? 'active' : ''}
              onMouseDown={(event) => { event.preventDefault(); pick(item); }}
            >
              <span>{item.label}</span>
              <small>{item.kind === 'movie' ? item.detail || 'Movie' : item.detail}</small>
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}
