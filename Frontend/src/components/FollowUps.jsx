import Icon from './Icon';

/** Keep only well-formed chips: {label, message} strings, at most `limit`. */
export function chipList(value, limit = 6) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((chip) => chip && typeof chip.label === 'string' && typeof chip.message === 'string' && chip.label.trim() && chip.message.trim())
    .slice(0, limit)
    .map((chip) => ({ label: chip.label.trim().slice(0, 80), message: chip.message.trim().slice(0, 300) }));
}

/**
 * Under the latest answer: one-tap answers to the question it asked ("Which Dune?"),
 * otherwise a few natural next steps. Tapping one sends it as the next message.
 */
function FollowUps({ options, suggestions, disabled, onPick }) {
  const answers = chipList(options);
  const next = answers.length ? [] : chipList(suggestions, 3);
  if (!answers.length && !next.length) return null;
  const chips = answers.length ? answers : next;
  return (
    <div
      className={`follow-ups${answers.length ? ' is-options' : ''}`}
      role="group"
      aria-label={answers.length ? 'Choose an answer' : 'Suggested follow-ups'}
    >
      {chips.map((chip) => (
        <button key={chip.message} type="button" disabled={disabled} onClick={() => onPick(chip.message)}>
          {!answers.length && <Icon name="arrowRight" size={13} />}
          <span>{chip.label}</span>
        </button>
      ))}
    </div>
  );
}

export default FollowUps;
