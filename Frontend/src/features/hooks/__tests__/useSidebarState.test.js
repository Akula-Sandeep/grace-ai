import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import useSidebarState from '../useSidebarState';

describe('useSidebarState', () => {
  it('collapses on desktop and opens on mobile', () => {
    const originalMatchMedia = window.matchMedia;
    const matchMedia = vi.fn()
      .mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
    window.matchMedia = matchMedia;
    const { result } = renderHook(() => useSidebarState());
    act(() => result.current.close());
    expect(result.current.collapsed).toBe(true);
    matchMedia.mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() });
    act(() => result.current.reopen());
    expect(result.current.open).toBe(true);
    window.matchMedia = originalMatchMedia;
  });
});
