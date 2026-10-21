import { lazy, Suspense } from 'react';
import Icon from './Icon';

const Markdown = lazy(() => import('./Markdown'));

/**
 * A list change the app verified (added, marked watched, a reminder set), shown under the
 * reply that made it, with its Undo. The reply itself is the model's own words; this card
 * is the app's record of what actually happened.
 */
function ActionNotice({ items }) {
  return items.map((text, index) => (
    <div className="memory-notice action-notice" role="status" key={`${index}-${text.slice(0, 24)}`}>
      <Icon name="check" size={14} />
      <div className="action-notice-text">
        <Suspense fallback={<span>{text}</span>}>
          <Markdown content={text} />
        </Suspense>
      </div>
    </div>
  ));
}

export default ActionNotice;
