import { useEffect, useState } from 'react';
import { currentSubscription, disablePush, enablePush, pushSupported } from '../lib/push';

const MESSAGES = {
  denied: 'Notifications are blocked for Grace in this browser. Allow them in the site settings, then try again.',
  unavailable: 'Notifications to devices are not set up on this server.',
  failed: 'Could not change notifications on this device. Please try again.',
};

/** "Notify me on this device": Web Push, opt-in per browser (Settings > Personalisation). */
export default function DeviceNotifications({ disabled = false }) {
  const supported = pushSupported();
  const [on, setOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!supported) return undefined;
    let live = true;
    currentSubscription()
      .then((subscription) => {
        if (live) setOn(Boolean(subscription));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [supported]);

  if (!supported) return null;

  async function toggle(enable) {
    setBusy(true);
    setMessage('');
    try {
      if (enable) {
        const outcome = await enablePush();
        setOn(outcome === 'on');
        if (outcome !== 'on') setMessage(MESSAGES[outcome]);
      } else {
        await disablePush();
        setOn(false);
      }
    } catch {
      setMessage(MESSAGES.failed);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <label className="memory-toggle">
        <input type="checkbox" checked={on} disabled={busy || disabled} onChange={(event) => toggle(event.target.checked)} />
        Send reminders and alerts to this device
      </label>
      {message && <p className="vx-settings-help" role="status">{message}</p>}
    </>
  );
}
