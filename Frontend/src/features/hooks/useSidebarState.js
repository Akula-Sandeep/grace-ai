import { useCallback, useState } from 'react';

const COLLAPSED_KEY = 'grace_sidebar_collapsed';
const MOBILE_QUERY = '(max-width: 760px)';

function readCollapsed() {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function writeCollapsed(value) {
  try {
    localStorage.setItem(COLLAPSED_KEY, value ? '1' : '0');
  } catch {
    // The preference is a convenience; storage may be unavailable.
  }
}

/**
 * Desktop: the sidebar collapses to an icon rail, and that choice is
 * remembered. Mobile: it is a drawer that opens over the content.
 */
function useSidebarState() {
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsedState] = useState(readCollapsed);

  const setCollapsed = useCallback((value) => {
    setCollapsedState(value);
    writeCollapsed(value);
  }, []);

  const close = useCallback(() => {
    if (window.matchMedia(MOBILE_QUERY).matches) setOpen(false);
    else setCollapsed(true);
  }, [setCollapsed]);

  const reopen = useCallback(() => {
    if (window.matchMedia(MOBILE_QUERY).matches) setOpen(true);
    else setCollapsed(false);
  }, [setCollapsed]);

  return {
    open,
    setOpen,
    collapsed,
    close,
    reopen,
  };
}

export default useSidebarState;
