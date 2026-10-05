/// <reference types="vitest/globals" />

import { bookFilterCounts, bookFilterOf, filterBooks, normalizeTab, TABS } from '../lib/consoleNav';

describe('normalizeTab (#1133)', () => {
  it('maps every current tab id onto itself', () => {
    for (const t of TABS) expect(normalizeTab(t.id).tab).toBe(t.id);
  });

  it('maps the pre-#1133 tab names onto where their content now lives', () => {
    expect(normalizeTab('overview')).toEqual({ tab: 'home' });
    expect(normalizeTab('scan')).toEqual({ tab: 'lab', lab: 'scan' });
    expect(normalizeTab('analysis')).toEqual({ tab: 'lab', lab: 'analysis' });
    expect(normalizeTab('positions')).toEqual({ tab: 'lab', lab: 'overview' });
    expect(normalizeTab('lab')).toEqual({ tab: 'lab', lab: 'overview' });
    expect(normalizeTab(' Books ')).toEqual({ tab: 'books' });
  });

  it('sends an unknown or missing name to Home, never to a blank page', () => {
    expect(normalizeTab('nonsense')).toEqual({ tab: 'home' });
    expect(normalizeTab('')).toEqual({ tab: 'home' });
    expect(normalizeTab(null)).toEqual({ tab: 'home' });
    expect(normalizeTab(undefined)).toEqual({ tab: 'home' });
  });
});

describe('book filters (#1133)', () => {
  const share = { status: 'ACTIVE', book_kind: 'share' as const };
  const options = { status: 'ACTIVE', book_kind: 'options' as const };
  const retiredOptions = { status: 'RETIRED', book_kind: 'options' as const };
  const retiredShare = { status: 'RETIRED', book_kind: 'share' as const };

  it('classifies by server status first, then book kind', () => {
    expect(bookFilterOf(share)).toBe('active');
    expect(bookFilterOf(options)).toBe('practice');
    expect(bookFilterOf(retiredOptions)).toBe('retired');
    expect(bookFilterOf(retiredShare)).toBe('retired');
  });

  it('keeps an unrecognised kind in Active, the default view', () => {
    const odd = { status: 'ACTIVE', book_kind: 'futures' } as unknown as typeof share;
    expect(bookFilterOf(odd)).toBe('active');
  });

  it('filters and counts so the three filters partition the list', () => {
    const books = [share, options, options, retiredOptions, retiredShare];
    expect(filterBooks(books, 'active')).toEqual([share]);
    expect(filterBooks(books, 'practice')).toHaveLength(2);
    expect(bookFilterCounts(books)).toEqual({ active: 1, practice: 2, retired: 2 });
  });
});
