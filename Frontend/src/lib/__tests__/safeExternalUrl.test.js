import { describe, expect, it } from 'vitest';
import { safeExternalUrl } from '../api';

describe('safeExternalUrl', () => {
  it('accepts absolute http(s) URLs only', () => {
    expect(safeExternalUrl('https://variety.com/a')).toBe('https://variety.com/a');
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl('')).toBeNull();
    expect(safeExternalUrl('/movie/1')).toBeNull();
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull();
  });
});
