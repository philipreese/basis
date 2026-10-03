import { describe, it, expect } from 'vitest';
import { colorModeLabel, nextColorMode, parseColorMode, resolveDark } from '../lib/theme';

describe('console color mode', () => {
  it('reads a stored preference and falls back to auto', () => {
    expect(parseColorMode('light')).toBe('light');
    expect(parseColorMode('dark')).toBe('dark');
    expect(parseColorMode('auto')).toBe('auto');
    expect(parseColorMode(null)).toBe('auto');
    expect(parseColorMode('true')).toBe('auto'); // a value from the old boolean toggle era
  });

  it('cycles auto, light, dark, then back to auto', () => {
    expect(nextColorMode('auto')).toBe('light');
    expect(nextColorMode('light')).toBe('dark');
    expect(nextColorMode('dark')).toBe('auto');
  });

  it('lets explicit modes override the OS and auto follow it', () => {
    expect(resolveDark('dark', false)).toBe(true);
    expect(resolveDark('light', true)).toBe(false);
    expect(resolveDark('auto', true)).toBe(true);
    expect(resolveDark('auto', false)).toBe(false);
  });

  it('labels the current choice', () => {
    expect(colorModeLabel('auto')).toBe('Auto');
    expect(colorModeLabel('light')).toBe('Light');
    expect(colorModeLabel('dark')).toBe('Dark');
  });
});
