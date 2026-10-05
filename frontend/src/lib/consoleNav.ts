import type { BookSummary } from './api';

// #1133: the console's five tabs. Share books, research and the options lab
// each get their own; every options-only surface (Scan, Analysis, Greek
// limits, market telemetry, the position list) lives inside the lab.
export type TabId = 'home' | 'books' | 'research' | 'lab' | 'settings';

export const TABS: readonly { id: TabId; label: string; short: string }[] = [
  { id: 'home',     label: 'Home',        short: 'Home' },
  { id: 'books',    label: 'Books',       short: 'Books' },
  { id: 'research', label: 'Research',    short: 'Research' },
  { id: 'lab',      label: 'Options lab', short: 'Lab' },
  { id: 'settings', label: 'Settings',    short: 'Settings' },
];

export type LabSection = 'overview' | 'scan' | 'analysis' | 'limits' | 'telemetry';

export interface NavTarget { tab: TabId; lab?: LabSection }

/**
 * Maps any tab name — a current id, or one of the pre-#1133 ids a server
 * `navigate_to` or an old link may still carry — onto a tab that exists.
 * Unknown names land on Home: a navigation that matched no tab used to
 * render a blank page, which reads as "everything is gone".
 */
export function normalizeTab(name: string | null | undefined): NavTarget {
  const key = (name ?? '').trim().toLowerCase();
  switch (key) {
    case 'home':
    case 'overview':
      return { tab: 'home' };
    case 'books':
      return { tab: 'books' };
    case 'research':
      return { tab: 'research' };
    case 'lab':
    case 'positions':
      return { tab: 'lab', lab: 'overview' };
    case 'scan':
      return { tab: 'lab', lab: 'scan' };
    case 'analysis':
      return { tab: 'lab', lab: 'analysis' };
    case 'settings':
      return { tab: 'settings' };
    default:
      return { tab: 'home' };
  }
}

// #1133: the Books tab's three filters. Server truth only: `status` says
// retired, `book_kind` says share vs options. A non-retired book that is not
// an options book falls into Active, so an unrecognised kind stays in the
// default view instead of hiding under Practice.
export type BookFilter = 'active' | 'practice' | 'retired';

export function bookFilterOf(book: Pick<BookSummary, 'status' | 'book_kind'>): BookFilter {
  if (book.status === 'RETIRED') return 'retired';
  if (book.book_kind === 'options') return 'practice';
  return 'active';
}

export function filterBooks<T extends Pick<BookSummary, 'status' | 'book_kind'>>(books: T[], filter: BookFilter): T[] {
  return books.filter(b => bookFilterOf(b) === filter);
}

export function bookFilterCounts(books: Pick<BookSummary, 'status' | 'book_kind'>[]): Record<BookFilter, number> {
  const counts: Record<BookFilter, number> = { active: 0, practice: 0, retired: 0 };
  for (const b of books) counts[bookFilterOf(b)] += 1;
  return counts;
}

// The research scorecard lives in the repo, not the served console, so the
// link goes to the file on GitHub.
export const SCORECARD_URL = 'https://github.com/philipreese/basis/blob/main/spec/research-scorecard.md';
