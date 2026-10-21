import { useCallback, useEffect, useRef } from 'react';

function ToastItem({ toast, onDismiss }) {
  const timerRef = useRef(null);
  const startedAtRef = useRef(0);
  const remainingRef = useRef(toast.duration);

  const startTimer = useCallback(() => {
    startedAtRef.current = Date.now();
    timerRef.current = window.setTimeout(() => onDismiss(toast.id), remainingRef.current);
  }, [onDismiss, toast.id]);

  useEffect(() => {
    startTimer();
    return () => window.clearTimeout(timerRef.current);
  }, [startTimer]);

  return (
    <div
      className={`toast${toast.kind === 'error' ? ' toast-error' : ''}`}
      role={toast.kind === 'error' ? 'alert' : 'status'}
      onMouseEnter={() => {
        window.clearTimeout(timerRef.current);
        remainingRef.current = Math.max(0, remainingRef.current - (Date.now() - startedAtRef.current));
      }}
      onMouseLeave={startTimer}
    >
      <span>{toast.message}</span>
      <button type="button" onClick={() => onDismiss(toast.id)} aria-label="Dismiss notification">×</button>
    </div>
  );
}

function ToastViewport({ toasts, onDismiss }) {
  if (!toasts.length) return null;
  return (
    <div className="toast-viewport" role="status" aria-live="polite" aria-atomic="false">
      {toasts.map((toast) => <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />)}
    </div>
  );
}

export default ToastViewport;
