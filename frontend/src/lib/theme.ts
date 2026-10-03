// Console color mode (#1038): presentation only, remembered per device.

export type ColorMode = 'auto' | 'light' | 'dark';

export const THEME_STORAGE_KEY = 'basis.theme';

// Browser chrome color: the header's Catppuccin crust (index.css), so the
// status bar and the sticky header read as one strip in either palette.
export const THEME_CHROME_COLOR = { dark: '#11111b', light: '#dce0e8' } as const;

const ORDER: readonly ColorMode[] = ['auto', 'light', 'dark'];

/** The stored preference, or 'auto' for anything missing or unrecognised. */
export function parseColorMode(raw: string | null): ColorMode {
  return ORDER.includes(raw as ColorMode) ? (raw as ColorMode) : 'auto';
}

/** The button cycles Auto → Light → Dark → Auto. */
export function nextColorMode(mode: ColorMode): ColorMode {
  return ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
}

/** Whether the dark palette applies: explicit modes win, Auto follows the OS. */
export function resolveDark(mode: ColorMode, osPrefersDark: boolean): boolean {
  return mode === 'dark' || (mode === 'auto' && osPrefersDark);
}

export function colorModeLabel(mode: ColorMode): string {
  return mode[0].toUpperCase() + mode.slice(1);
}
