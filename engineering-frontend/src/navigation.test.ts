import { describe, expect, it } from 'vitest';
import { isNavigationActive } from './navigation';

describe('isNavigationActive', () => {
  it('keeps imports active on an import detail page only', () => {
    expect(isNavigationActive('/imports/123', '/')).toBe(true);
    expect(isNavigationActive('/categories', '/')).toBe(false);
  });

  it('matches the selected workspace without prefix collisions', () => {
    expect(isNavigationActive('/data', '/data')).toBe(true);
    expect(isNavigationActive('/categories', '/categories')).toBe(true);
    expect(isNavigationActive('/categories/archive', '/categories')).toBe(true);
    expect(isNavigationActive('/rules', '/categories')).toBe(false);
  });
});
