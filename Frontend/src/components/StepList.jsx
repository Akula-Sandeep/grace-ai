import Icon from './Icon';

/**
 * What Grace did for an answer (B4): "Looking up Arrival", "Checking where it streams".
 * While it works, the steps show as a short live list; once the answer is written they
 * fold into a one-line disclosure under it.
 */
function StepList({ steps, working }) {
  if (!Array.isArray(steps) || steps.length === 0) return null;
  if (working) {
    return (
      <ol className="step-list is-working" aria-label="Progress">
        {steps.map((step, index) => {
          const current = index === steps.length - 1;
          return (
            <li key={`${index}-${step}`} className={current ? 'is-current' : 'is-done'} aria-current={current ? 'step' : undefined}>
              {current ? <span className="step-spinner" aria-hidden="true" /> : <Icon name="check" size={13} />}
              <span>{step}</span>
            </li>
          );
        })}
      </ol>
    );
  }
  if (steps.length < 2) return null; // one step ("Thinking") says nothing worth keeping
  return (
    <details className="step-list is-done">
      <summary>{steps.length} steps</summary>
      <ol>
        {steps.map((step, index) => (
          <li key={`${index}-${step}`}>
            <Icon name="check" size={13} />
            <span>{step}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

/** Add a status label to a step list, skipping repeats of the current step. */
export function addStep(steps, label) {
  const clean = String(label || '').trim().slice(0, 120);
  if (!clean) return steps || [];
  const list = steps || [];
  if (list[list.length - 1] === clean) return list;
  return [...list, clean].slice(-8);
}

export default StepList;
