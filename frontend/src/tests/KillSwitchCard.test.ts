/// <reference types="vitest/globals" />

import { render, screen, fireEvent } from '@testing-library/svelte';
import KillSwitchCard from '../lib/KillSwitchCard.svelte';
import type { TradingControlView } from '../lib/api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function view(state: 'ACTIVE' | 'HALT_ENTRIES', sentinel = false): TradingControlView {
  return {
    sentinel_halt: sentinel,
    controls: [{ scope: 'GLOBAL', state, reason: 'e2e drill', actor: 'console', changed_at: '2026-10-05T12:00:00+00:00' }],
  };
}

describe('KillSwitchCard (#1133)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('halts GLOBAL entries with a typed reason, and the confirm stays disabled without one', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(view('ACTIVE')))
      .mockResolvedValueOnce(jsonResponse(view('HALT_ENTRIES')));
    render(KillSwitchCard);

    await fireEvent.click(await screen.findByTestId('kill-switch-action'));
    const confirm = screen.getByTestId('kill-switch-confirm');
    expect(confirm).toBeDisabled();
    await fireEvent.input(screen.getByTestId('kill-switch-reason'), { target: { value: '   ' } });
    expect(confirm).toBeDisabled();
    await fireEvent.input(screen.getByTestId('kill-switch-reason'), { target: { value: 'phone drill' } });
    await fireEvent.click(confirm);

    const req = fetchSpy.mock.calls[1][0] as Request;
    expect(req.method).toBe('POST');
    expect(await req.clone().json()).toEqual({ scope: 'GLOBAL', state: 'HALT_ENTRIES', reason: 'phone drill', ack: null });
    expect(await screen.findByTestId('kill-switch-halted')).toHaveTextContent('GLOBAL HALT_ENTRIES');
  });

  it('never offers a resume — a halted state points to Home instead', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(view('HALT_ENTRIES')));
    const onGoHome = vi.fn();
    render(KillSwitchCard, { props: { onGoHome } });

    const halted = await screen.findByTestId('kill-switch-halted');
    expect(halted).toHaveTextContent('e2e drill');
    expect(screen.queryByTestId('kill-switch-action')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /resume$/i })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('kill-switch-go-home'));
    expect(onGoHome).toHaveBeenCalledOnce();
  });

  it('reads a sentinel file or a missing GLOBAL row as halted, never as a safe ACTIVE', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(view('ACTIVE', true)));
    render(KillSwitchCard);
    expect(await screen.findByTestId('kill-switch-halted')).toHaveTextContent('SENTINEL HALT');
  });

  it('a missing GLOBAL row reads as halted', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ controls: [], sentinel_halt: false }));
    render(KillSwitchCard);
    expect(await screen.findByTestId('kill-switch-halted')).toHaveTextContent('state unknown');
  });

  it('says so when the control state fails to load', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: 'boom' }, 500));
    render(KillSwitchCard);
    expect(await screen.findByText(/failed to load/)).toBeInTheDocument();
  });
});
