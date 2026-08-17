<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getBooks, getAuditEvents,
    type BookSummary, type AuditEvent, type LiveGateChecklist,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  let books        = $state<BookSummary[]>([]);
  let events       = $state<AuditEvent[]>([]);
  let isLoading    = $state(true);
  let filterBook   = $state('');
  let filterDate   = $state('');
  let filterType   = $state('');
  let expandedEvent = $state<number | null>(null);

  onMount(async () => {
    try {
      books = await getBooks();
      await loadEvents();
    } catch (e: unknown) {
      toast('Failed to load books: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      isLoading = false;
    }
  });

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
  <section>
    <div class="flex items-baseline justify-between mb-4">
      <h2 class="text-xl font-bold text-ctp-text tracking-tight">Lab Books</h2>
      <p class="text-xs text-ctp-overlay0">Live Gate: ≥30 trades · ≥3 months · zero breaches · expectancy ≥ 0 after haircut</p>
    </div>

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
                <td class="px-3 py-2">
                  <span class="font-bold text-ctp-text">{book.id}</span>
                  {#if book.control_state !== 'ACTIVE'}
                    <span class="ml-1 text-ctp-red font-bold" title={book.control_state}>⛔</span>
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
      </div>
    </div>

    {#if events.length === 0}
      <div class="carbon-card p-8 text-center text-ctp-overlay0 text-sm" data-testid="audit-empty">
        No audit events match. The executor writes these every run — order lifecycle, control checks, anomalies.
      </div>
    {:else}
      <div class="carbon-card divide-y divide-ctp-surface0/50 text-xs carbon-mono" data-testid="audit-list">
        {#each events as ev (ev.id)}
          <button
            class="w-full text-left px-4 py-2 hover:bg-ctp-surface0/30 transition flex flex-wrap items-baseline gap-x-3"
            onclick={() => (expandedEvent = expandedEvent === ev.id ? null : ev.id)}
          >
            <span class="text-ctp-overlay0 whitespace-nowrap">{ev.run_at.slice(0, 16).replace('T', ' ')}</span>
            <span class="font-bold {ev.event_type.includes('REJECT') || ev.event_type.includes('SHOCK') || ev.event_type.includes('BREACH') || ev.event_type.includes('LOST') ? 'text-ctp-red' : 'text-ctp-text'}">
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
