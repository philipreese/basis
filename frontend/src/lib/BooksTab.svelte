<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getBooks, getAuditEvents, updateTradingControl, getTradingControl,
    type BookSummary, type AuditEvent, type LiveGateChecklist, type TradingControlView,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import ReconciliationPanel from './ReconciliationPanel.svelte';

  // A resolution correction mutates positions and cash the OVERVIEW renders
  // (#354): without this, a recorded external close leaves the Overview
  // showing the position OPEN with a stale P1 alert whose Close button 400s.
  let { onDataChanged = () => {} }: { onDataChanged?: () => void | Promise<void> } = $props();

  let books        = $state<BookSummary[]>([]);
  let events       = $state<AuditEvent[]>([]);
  let control      = $state<TradingControlView | null>(null);
  let isLoading    = $state(true);
  let filterBook   = $state('');
  let filterDate   = $state('');
  let filterType   = $state('');
  let showUrgentOnly = $state(false);
  let expandedEvent = $state<number | null>(null);

  // Beside the ⛔ indicator, WHY it's halted (#474): reason/actor/changed_at
  // are fetched but were dropped everywhere — a book can sit halted for
  // weeks with no way in the console to learn why.
  const controlFor = (bookId: string) => control?.controls.find(c => c.scope === bookId) ?? null;

  const filteredEvents = $derived(showUrgentOnly ? events.filter(e => e.urgent) : events);

  onMount(async () => {
    try {
      books = await getBooks();
      await loadEvents();
    } catch (e: unknown) {
      toast('Failed to load books: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      isLoading = false;
    }
    // Separate try/catch (#474 review): a trading-control outage must not
    // blank the whole Books tab — it only means halt reasons go unlabeled.
    await loadControl();
  });

  async function loadControl() {
    try {
      control = await getTradingControl();
    } catch (e: unknown) {
      toast('Failed to load control state: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  async function loadEvents() {
    try {
      events = await getAuditEvents({
        book_id: filterBook || undefined,
        date: filterDate || undefined,
        event_type: filterType.trim() || undefined,
        limit: 200,
      });
    } catch (e: unknown) {
      toast('Failed to load audit events: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  function selectBook(id: string) {
    filterBook = filterBook === id ? '' : id;
    loadEvents();
  }

  // Per-book kill switch (#279): HALT from here, RESUME console-only-but-
  // here-IS-the-console. Same typed-reason contract as the status strip.
  let controlTarget = $state<{ id: string; toState: 'ACTIVE' | 'HALT_ENTRIES' } | null>(null);
  let controlReason = $state('');
  let controlBusy   = $state(false);

  function openControl(e: MouseEvent, book: BookSummary) {
    e.stopPropagation(); // the row click filters the audit trail — not this
    controlTarget = { id: book.id, toState: book.control_state === 'ACTIVE' ? 'HALT_ENTRIES' : 'ACTIVE' };
    controlReason = '';
  }

  async function submitControl(e: SubmitEvent) {
    e.preventDefault();
    if (!controlTarget || controlReason.trim().length < 3) return;
    controlBusy = true;
    try {
      await updateTradingControl(controlTarget.id, controlTarget.toState, controlReason.trim());
      toast(`${controlTarget.id} → ${controlTarget.toState}`, 'success', 4000);
      controlTarget = null;
      books = await getBooks();
      await loadControl();
    } catch (err: unknown) {
      toast('Control change failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      controlBusy = false;
    }
  }

  function gateCells(g: LiveGateChecklist): { label: string; ok: boolean }[] {
    return [
      { label: g.trades_ok ? '✓ trades' : `${g.closed_trades}/${g.closed_trades_required} trades`, ok: g.trades_ok },
      { label: g.months_ok ? '✓ 3mo' : `${g.months_elapsed.toFixed(1)}/${g.months_required}mo`, ok: g.months_ok },
      { label: g.breaches_ok ? '✓ 0 breach' : `${g.breaches} breach`, ok: g.breaches_ok },
      {
        label: g.expectancy_ok
          ? '✓ expectancy'
          : g.expectancy_after_haircut === null ? 'exp —' : `exp ${g.expectancy_after_haircut.toFixed(0)}`,
        ok: g.expectancy_ok,
      },
    ];
  }

  const fmtPct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);
  const fmtNum = (v: number | null) => (v === null ? '—' : v.toFixed(2));
</script>

<div class="space-y-8 mt-2">
  <!-- Drift banner + audited correction tools (#310); a one-liner when clean -->
  <ReconciliationPanel onCorrectionApplied={async () => { books = await getBooks(); await onDataChanged(); }} />

  <section>
    <div class="flex items-baseline justify-between mb-4">
      <h2 class="text-xl font-bold text-ctp-text tracking-tight">Lab Books</h2>
      <p class="text-xs text-ctp-overlay0">Live Gate: ≥30 trades · ≥3 months · zero breaches · expectancy ≥ 0 after haircut</p>
    </div>

    {#if controlTarget}
      <form onsubmit={submitControl} class="flex items-center gap-2 mb-3">
        <span class="font-bold text-xs {controlTarget.toState === 'ACTIVE' ? 'text-ctp-green' : 'text-ctp-red'}">
          {controlTarget.toState === 'ACTIVE' ? 'RESUME' : 'HALT'} {controlTarget.id} —
        </span>
        <!-- svelte-ignore a11y_autofocus -->
        <input type="text" bind:value={controlReason} autofocus data-testid="book-control-reason"
               placeholder="reason (required, min 3 chars)"
               class="flex-1 max-w-md px-2 py-1 text-xs border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve" />
        <button type="submit" disabled={controlReason.trim().length < 3 || controlBusy}
                data-testid="book-control-confirm"
                class="px-2 py-1 text-xs font-bold rounded bg-ctp-surface0 text-ctp-text disabled:opacity-40">
          Confirm
        </button>
        <button type="button" class="text-xs text-ctp-overlay0 hover:underline"
                onclick={() => (controlTarget = null)}>cancel</button>
      </form>
    {/if}

    {#if isLoading}
      <div class="carbon-card p-10 text-center text-ctp-overlay0">Loading books…</div>
    {:else if books.length === 0}
      <div class="carbon-card p-10 text-center">
        <p class="text-ctp-subtext0 font-medium">No lab books yet.</p>
        <p class="text-ctp-overlay0 text-xs mt-1">Books are seeded when the executor database initializes.</p>
      </div>
    {:else}
      <div class="carbon-card overflow-x-auto">
        <table class="w-full text-xs carbon-mono" data-testid="books-table">
          <thead>
            <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
              <th class="px-3 py-2">Book</th>
              <th class="px-3 py-2">Config</th>
              <th class="px-3 py-2 text-right">P&L</th>
              <th class="px-3 py-2 text-right">Trades</th>
              <th class="px-3 py-2 text-right">Win rate</th>
              <th class="px-3 py-2 text-right">Expectancy*</th>
              <th class="px-3 py-2 text-right">Max DD</th>
              <th class="px-3 py-2 text-right">Deployed</th>
              <th class="px-3 py-2 text-right">Pos</th>
              <th class="px-3 py-2">Live Gate</th>
            </tr>
          </thead>
          <tbody>
            {#each books as book (book.id)}
              <tr
                class="border-b border-ctp-surface0/50 cursor-pointer transition
                  {filterBook === book.id ? 'bg-ctp-mauve/10' : 'hover:bg-ctp-surface0/30'}"
                onclick={() => selectBook(book.id)}
              >
                <td class="px-3 py-2 whitespace-nowrap">
                  <span class="font-bold text-ctp-text">{book.id}</span>
                  {#if book.control_state !== 'ACTIVE'}
                    <span class="ml-1 text-ctp-red font-bold"
                          title={controlFor(book.id) ? `${controlFor(book.id)?.reason} — by ${controlFor(book.id)?.actor} · ${controlFor(book.id)?.changed_at}` : book.control_state}>
                      ⛔
                    </span>
                    {#if controlFor(book.id)}
                      <span class="ml-1 text-[10px] text-ctp-overlay0" data-testid="book-halt-reason-{book.id}">
                        {controlFor(book.id)?.reason}
                      </span>
                    {/if}
                    <button class="ml-1 text-[10px] text-ctp-green font-bold hover:underline"
                            data-testid="resume-{book.id}"
                            onclick={(e) => openControl(e, book)}>RESUME</button>
                  {:else}
                    <button class="ml-1 text-[10px] text-ctp-red font-bold hover:underline"
                            data-testid="halt-{book.id}"
                            onclick={(e) => openControl(e, book)}>HALT</button>
                  {/if}
                </td>
                <td class="px-3 py-2 text-ctp-subtext0">
                  {book.engine_variant}/{book.underlying}
                  <span class="text-ctp-overlay0" title="config hash v{book.config_version}">·{book.config_hash.slice(0, 8)}</span>
                </td>
                <td class="px-3 py-2 text-right font-bold {book.pnl >= 0 ? 'text-ctp-green' : 'text-ctp-red'}">
                  {book.pnl >= 0 ? '+' : ''}{book.pnl.toFixed(0)}
                </td>
                <td class="px-3 py-2 text-right">{book.closed_trades}</td>
                <td class="px-3 py-2 text-right">
                  {fmtPct(book.win_rate)}
                  {#if book.win_rate !== null}<span class="text-ctp-overlay0"> (n={book.closed_trades})</span>{/if}
                </td>
                <td class="px-3 py-2 text-right">{fmtNum(book.expectancy_after_haircut)}</td>
                <td class="px-3 py-2 text-right text-ctp-red">{book.max_drawdown > 0 ? `-${book.max_drawdown.toFixed(0)}` : '0'}</td>
                <td class="px-3 py-2 text-right">{book.deployed_pct.toFixed(0)}%</td>
                <td class="px-3 py-2 text-right">{book.open_positions}/{book.max_positions}</td>
                <td class="px-3 py-2">
                  <div class="flex flex-wrap gap-1">
                    {#each gateCells(book.live_gate) as cell}
                      <span class="px-1.5 py-0.5 rounded text-[10px] font-bold
                        {cell.ok ? 'bg-ctp-green/15 text-ctp-green' : 'bg-ctp-surface0 text-ctp-overlay0'}">
                        {cell.label}
                      </span>
                    {/each}
                    {#if book.live_gate.eligible}
                      <span class="px-1.5 py-0.5 rounded text-[10px] font-black bg-ctp-green text-ctp-crust">ELIGIBLE</span>
                    {/if}
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="text-[10px] text-ctp-overlay0 mt-2">
        * mean realized P&L per closed trade after the $5/contract slippage haircut (paper fills are optimistic — ADR-0007).
        Click a row to filter the audit trail below.
      </p>
    {/if}
  </section>

  <section class="border-t border-ctp-surface0 pt-8">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
      <h2 class="text-xl font-bold text-ctp-text tracking-tight">Audit Trail</h2>
      <div class="flex items-center gap-2 text-xs">
        <select bind:value={filterBook} onchange={loadEvents} data-testid="audit-filter-book"
                class="px-2 py-1 border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text carbon-mono">
          <option value="">all books</option>
          {#each books as book (book.id)}<option value={book.id}>{book.id}</option>{/each}
        </select>
        <input type="date" bind:value={filterDate} onchange={loadEvents} data-testid="audit-filter-date"
               class="px-2 py-1 border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text carbon-mono" />
        <input type="text" bind:value={filterType} onchange={loadEvents} placeholder="event type"
               data-testid="audit-filter-type"
               class="px-2 py-1 w-36 border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text carbon-mono" />
        <label class="flex items-center gap-1 text-ctp-subtext0 cursor-pointer">
          <input type="checkbox" bind:checked={showUrgentOnly} data-testid="audit-urgent-only" class="accent-ctp-red" />
          urgent only
        </label>
      </div>
    </div>

    {#if filteredEvents.length === 0}
      <div class="carbon-card p-8 text-center text-ctp-overlay0 text-sm" data-testid="audit-empty">
        {#if events.length === 0}
          No audit events match. The executor writes these every run — order lifecycle, control checks, anomalies.
        {:else}
          No urgent events match the current filters.
        {/if}
      </div>
    {:else}
      <div class="carbon-card divide-y divide-ctp-surface0/50 text-xs carbon-mono" data-testid="audit-list">
        {#each filteredEvents as ev (ev.id)}
          <button
            class="w-full text-left px-4 py-2 transition flex flex-wrap items-baseline gap-x-3
              {ev.urgent ? 'bg-ctp-red/10 hover:bg-ctp-red/15' : 'hover:bg-ctp-surface0/30'}"
            onclick={() => (expandedEvent = expandedEvent === ev.id ? null : ev.id)}
          >
            <span class="text-ctp-overlay0 whitespace-nowrap">{ev.run_at.slice(0, 16).replace('T', ' ')}</span>
            {#if ev.urgent}<span class="text-ctp-red font-black" title="urgent — needs a human">⚠</span>{/if}
            <span class="font-bold {ev.urgent ? 'text-ctp-red' : 'text-ctp-text'}">
              {ev.event_type}
            </span>
            {#if ev.book_id}<span class="text-ctp-mauve">{ev.book_id}</span>{/if}
            <span class="text-ctp-overlay0">by {ev.actor}</span>
            {#if expandedEvent === ev.id}
              <pre class="w-full mt-1 p-2 rounded bg-ctp-crust text-ctp-subtext0 overflow-x-auto text-[10px]">{JSON.stringify(ev.payload, null, 2)}</pre>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
  </section>
</div>
