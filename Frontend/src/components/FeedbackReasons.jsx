import { useEffect, useRef, useState } from 'react';
import Icon from './Icon';

export const FEEDBACK_REASONS = [
  { id: 'wrong_film', label: 'Wrong film' },
  { id: 'wrong_facts', label: 'Wrong facts' },
  { id: 'ignored_request', label: 'Ignored what I asked' },
  { id: 'too_long', label: 'Too long' },
  { id: 'not_useful', label: 'Not useful' },
  { id: 'other', label: 'Something else' },
];

/**
 * Asked right after a thumbs down: what went wrong, in one tap, with an optional note.
 * The answer is used to find and fix the failure; skipping is always fine.
 */
function FeedbackReasons({ onSubmit, onDismiss }) {
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const firstRef = useRef(null);
  // The parent passes a new function each render; the thank-you timer must not restart on that.
  const dismissRef = useRef(onDismiss);
  useEffect(() => {
    dismissRef.current = onDismiss;
  }, [onDismiss]);

  useEffect(() => {
    firstRef.current?.focus();
    const escape = (event) => {
      if (event.key === 'Escape') dismissRef.current();
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, []);

  useEffect(() => {
    if (!sent) return undefined;
    const timer = window.setTimeout(() => dismissRef.current(), 1800);
    return () => window.clearTimeout(timer);
  }, [sent]);

  if (sent) {
    return (
      <p className="feedback-reasons feedback-thanks" role="status">
        <Icon name="check" size={14} /> Thanks — that helps fix it.
      </p>
    );
  }

  async function submit(event) {
    event.preventDefault();
    if (!reason || sending) return;
    setSending(true);
    const ok = await onSubmit({ reason, note: note.trim() || null });
    setSending(false);
    if (ok !== false) setSent(true);
  }

  return (
    <form
      className="feedback-reasons"
      aria-label="What went wrong with this answer?"
      onSubmit={submit}
    >
      <div className="feedback-reasons-head">
        <span id="feedback-reasons-title">What went wrong?</span>
        <button type="button" className="feedback-reasons-close" onClick={onDismiss} aria-label="Skip">
          <Icon name="close" size={14} />
        </button>
      </div>
      <div className="feedback-reasons-chips" role="radiogroup" aria-labelledby="feedback-reasons-title">
        {FEEDBACK_REASONS.map((option, index) => (
          <button
            key={option.id}
            ref={index === 0 ? firstRef : undefined}
            type="button"
            role="radio"
            aria-checked={reason === option.id}
            className={reason === option.id ? 'is-selected' : ''}
            onClick={() => setReason(option.id)}
          >
            {option.label}
          </button>
        ))}
      </div>
      {reason && (
        <div className="feedback-reasons-note">
          <label className="sr-only" htmlFor="feedback-note">Anything to add?</label>
          <textarea
            id="feedback-note"
            rows={2}
            maxLength={1000}
            value={note}
            placeholder={reason === 'other' ? 'What went wrong?' : 'Anything to add? (optional)'}
            onChange={(event) => setNote(event.target.value)}
          />
          <button type="submit" className="feedback-reasons-send" disabled={sending}>
            {sending ? 'Sending…' : 'Send'}
          </button>
        </div>
      )}
    </form>
  );
}

export default FeedbackReasons;
