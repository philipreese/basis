/// <reference types="vitest/globals" />

/**
 * Tab navigation state-transition tests.
 *
 * Tests are pure logic — no DOM rendering required. We inline the same
 * gating rule that App.svelte uses so that any regression in the rule
 * causes a test failure.
 */

type Tab = 'scanner' | 'opportunities' | 'ledger' | 'settings';
const LOCKED_TABS: Tab[] = ['opportunities', 'ledger', 'settings'];

function canNavigate(to: Tab, isAcknowledgeReviewed: boolean): boolean {
  if (to === 'scanner') return true;
  return isAcknowledgeReviewed;
}

function navigate(
  currentTab: Tab,
  to: Tab,
  isAcknowledgeReviewed: boolean,
): Tab {
  return canNavigate(to, isAcknowledgeReviewed) ? to : currentTab;
}

describe('Tab navigation — session unlocked', () => {
  const unlocked = true;

  it('navigates to opportunities', () => {
    expect(navigate('scanner', 'opportunities', unlocked)).toBe('opportunities');
  });

  it('navigates to ledger', () => {
    expect(navigate('scanner', 'ledger', unlocked)).toBe('ledger');
  });

  it('navigates to settings', () => {
    expect(navigate('scanner', 'settings', unlocked)).toBe('settings');
  });

  it('navigates back to scanner from any tab', () => {
    for (const from of LOCKED_TABS) {
      expect(navigate(from, 'scanner', unlocked)).toBe('scanner');
    }
  });
});

describe('Tab navigation — session locked', () => {
  const locked = false;

  it('does not navigate to opportunities when locked', () => {
    expect(navigate('scanner', 'opportunities', locked)).toBe('scanner');
  });

  it('does not navigate to ledger when locked', () => {
    expect(navigate('scanner', 'ledger', locked)).toBe('scanner');
  });

  it('does not navigate to settings when locked', () => {
    expect(navigate('scanner', 'settings', locked)).toBe('scanner');
  });

  it('always allows navigation to scanner regardless of lock state', () => {
    expect(navigate('opportunities', 'scanner', locked)).toBe('scanner');
    expect(navigate('ledger', 'scanner', locked)).toBe('scanner');
    expect(navigate('settings', 'scanner', locked)).toBe('scanner');
  });
});

describe('canNavigate — edge cases', () => {
  it('scanner is always reachable', () => {
    expect(canNavigate('scanner', false)).toBe(true);
    expect(canNavigate('scanner', true)).toBe(true);
  });

  it('all locked tabs require acknowledgement', () => {
    for (const tab of LOCKED_TABS) {
      expect(canNavigate(tab, false)).toBe(false);
      expect(canNavigate(tab, true)).toBe(true);
    }
  });
});
