<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getBooks, getAuditEvents, updateTradingControl, getTradingControl,
    type BookSummary, type AuditEvent, type LiveGateChecklist, type TradingControlView, type TailHedgeMetrics,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { formatLocalDateTime } from './formatters';
  import ReconciliationPanel from './ReconciliationPanel.svelte';
  import FlexAuditPanel from './FlexAuditPanel.svelte';
  import LiveOrdersPanel from './LiveOrdersPanel.svelte';
  import { startPolling } from './poll';

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
  let expandedEvent = $state<number | null>(null);

  // #603: the trail runs to ~200 rows on a busy night, most of it routine
  // per-run housekeeping — an operator scanning during an incident needs to
  // get past that without reading every row.
  type Severity = 'urgent' | 'notable' | 'routine';

  // High-volume, expected-every-run housekeeping the executor writes on its
  // own — never needs a human glance by itself. Deliberately a narrow
  // allowlist, not a denylist: an event type NOT on this list defaults to
  // 'notable' and stays visible, so an unrecognized (possibly new/important)
  // type is never silently swept into the routine bucket.
  const ROUTINE_EVENT_TYPES = new Set([
    'ENTRY_FILLED', 'CLOSE_FILLED', 'ORDER_SUBMITTED', 'ORDER_STAGED',
    'RUN_LOCK_HELD', 'BOOK_CONFIG_SYNCED', 'PLAYBOOK_SYNCED',
    'DIGEST_COMPOSED', 'FLEX_AUDIT', 'NTFY_COMMANDS_POLLED',
    'STAGED_ORDER_FOUND_RESTING', 'EXECUTOR_HOLIDAY_SKIP',
    'CANDIDATE_UNPRICEABLE', 'SCAN_BLOCKED', 'CONTROL_CHECK',
  ]);

  function severityOf(ev: AuditEvent): Severity {
    if (ev.urgent) return 'urgent'; // server truth (#474) — never overridden client-side
    return ROUTINE_EVENT_TYPES.has(ev.event_type) ? 'routine' : 'notable';
  }

  const severityCls: Record<Severity, string> = {
    urgent: 'text-ctp-red',
    notable: 'text-ctp-text',
    routine: 'text-ctp-overlay0',
  };

  let severityFilter = $state<'all' | 'notable-up' | 'urgent'>('all');
  let groupByType     = $state(false);
  let expandedGroups   = $state<Set<string>>(new Set());

  // Beside the ⛔ indicator, WHY it's halted (#474): reason/actor/changed_at
  // are fetched but were dropped everywhere — a book can sit halted for
  // weeks with no way in the console to learn why.
  const controlFor = (bookId: string) => control?.controls.find(c => c.scope === bookId) ?? null;

  const filteredEvents = $derived(
    events.filter(e => {
      const sev = severityOf(e);
      if (severityFilter === 'urgent') return sev === 'urgent';
      if (severityFilter === 'notable-up') return sev !== 'routine';
      return true;
    }),
  );

  interface EventGroup { event_type: string; events: AuditEvent[]; severity: Severity; latest: string }

  // Grouped-by-type view (#603): a busy night can carry dozens of the same
  // routine event_type — collapsing them to one row with a count lets an
  // operator scan event TYPES first, then drill into one that matters.
  // filteredEvents is already newest-first (server-sorted), so each group's
  // first member is its latest row.
  const groupedEvents = $derived.by((): EventGroup[] => {
    const byType = new Map<string, AuditEvent[]>();
    for (const ev of filteredEvents) {
      const arr = byType.get(ev.event_type);
      if (arr) arr.push(ev); else byType.set(ev.event_type, [ev]);
    }
    return [...byType.entries()]
      .map(([event_type, evs]): EventGroup => ({
        event_type,
        events: evs,
        severity: evs.some(e => severityOf(e) === 'urgent')
          ? 'urgent'
          : evs.some(e => severityOf(e) === 'notable') ? 'notable' : 'routine',
        latest: evs[0].run_at,
      }))
      .sort((a, b) => (a.latest < b.latest ? 1 : a.latest > b.latest ? -1 : 0));
  });

  function toggleGroup(eventType: string) {
    const next = new Set(expandedGroups);
    if (next.has(eventType)) next.delete(eventType); else next.add(eventType);
    expandedGroups = next;
  }

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

    // Live Gate progress, control state, and halt reasons are all
    // page-load snapshots otherwise (#477) — a console left open on Books
    // shows an ACTIVE book through tonight's drift halt forever.
    startPolling(async () => {
      if (controlTarget !== null) return; // don't yank rows out from under an open HALT/RESUME form
      try {
        books = await getBooks();
      } catch {
        /* keep last-known books; background poll failures stay silent */
      }
      await loadControl({ silent: true });
    });
  });

  async function loadControl(opts: { silent?: boolean } = {}) {
    try {
      control = await getTradingControl();
    } catch (e: unknown) {
      if (!opts.silent) toast('Failed to load control state: ' + (e instanceof Error ? e.message : String(e)), 'error');
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

  type GateCellStatus = 'ok' | 'fail' | 'pending';
  type GateCell = { label: string; status: GateCellStatus; title?: string };

  // #655: the original ADR-0006 four render ok/fail as before; the
  // ADR-0010 conditions (additional_conditions) add a THIRD, visually
  // distinct 'pending' state — not_yet_evaluated must never look like a
  // pass (green) or blend into an ordinary fail (the existing neutral
  // fail styling), or an operator scanning the row could read a
  // materially weaker standard as the real ADR-0010 bar.
  function gateCells(g: LiveGateChecklist): GateCell[] {
    const base: GateCell[] = [
      { label: g.trades_ok ? '✓ trades' : `${g.closed_trades}/${g.closed_trades_required} trades`, status: g.trades_ok ? 'ok' : 'fail' },
      { label: g.months_ok ? '✓ 3mo' : `${g.months_elapsed.toFixed(1)}/${g.months_required}mo`, status: g.months_ok ? 'ok' : 'fail' },
      { label: g.breaches_ok ? '✓ 0 breach' : `${g.breaches} breach`, status: g.breaches_ok ? 'ok' : 'fail' },
      {
        // #656: the bar is expectancy − 1·SE ≥ 0, not a point estimate —
        // the interval renders even on a pass, so the margin is always
        // visible, not just the fact of clearing it.
        label: g.expectancy_after_haircut === null
          ? 'exp —'
          : `${g.expectancy_ok ? '✓ ' : ''}exp ${fmtInterval(g.expectancy_after_haircut, g.expectancy_se)}`,
        status: g.expectancy_ok ? 'ok' : 'fail',
        title: 'expectancy ± 1 standard error, after the $5/contract haircut',
      },
    ];
    const additional: GateCell[] = g.additional_conditions.map((c) => ({
      label: c.status === 'ok' ? `✓ ${c.label}` : c.status === 'not_yet_evaluated' ? `${c.label} …` : `✗ ${c.label}`,
      status: c.status === 'not_yet_evaluated' ? 'pending' : c.status,
      title: c.detail || undefined,
    }));
    return [...base, ...additional];
  }

  const gateCellClass: Record<GateCellStatus, string> = {
    ok: 'bg-ctp-green/15 text-ctp-green',
    fail: 'bg-ctp-surface0 text-ctp-overlay0',
    pending: 'bg-ctp-yellow/10 text-ctp-yellow border border-dashed border-ctp-yellow/40',
  };

  const fmtPct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(0)}%`);

  // ADR-0012 (#772): the tail-hedge sleeve is judged on convexity, never
  // expectancy — a book carrying tail_hedge_metrics renders these THREE
  // numbers in place of the standard win-rate/expectancy cells, and its
  // Live Gate row still shows (permanently ineligible, per the backend).
  const fmtBleed = (v: number | null) => (v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%/mo`);
  const fmtStress = (m: TailHedgeMetrics) =>
    m.stress_episode_status === 'no_episode_yet'
      ? 'no episode yet'
      : `${m.stress_episode_payoff! >= 0 ? '+' : ''}${m.stress_episode_payoff!.toFixed(0)}`;
  const fmtContribution = (v: number | null) => (v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(0)}`);
  // #656: expectancy renders as an interval, x ± se, everywhere it appears
  // — se is None below n=2 (undefined, not zero), so the ± term is omitted
  // rather than shown as "± 0.00", which would misstate a real trade as
  // having no uncertainty.
  const fmtInterval = (v: number | null, se: number | null) =>
    v === null ? '—' : se === null ? v.toFixed(2) : `${v.toFixed(2)} ± ${se.toFixed(2)}`;
</script>

<div class="space-y-8 mt-2">
  <!-- Drift banner + audited correction tools (#310); a one-liner when clean -->
  <ReconciliationPanel onCorrectionApplied={async () => { books = await getBooks(); await onDataChanged(); }} />

  <!-- Weekly Flex-audit discrepancies awaiting acknowledgment (#571) -->
  <FlexAuditPanel />

  <!-- What's currently resting at the broker (#601) -->
  <LiveOrdersPanel />

  <section>
    <div class="flex items-baseline justify-between mb-4">
      <h2 class="text-xl font-bold text-ctp-text tracking-tight">Lab Books</h2>
      <p class="text-xs text-ctp-overlay0">Live Gate: ≥30 trades · ≥3 months · zero breaches · expectancy − 1 SE ≥ 0 after haircut (interim floor, ADR-0010) · plus ADR-0010 conditions (pending, #215)</p>
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
                          title={controlFor(book.id) ? `${controlFor(book.id)?.reason} — by ${controlFor(book.id)?.actor} · ${formatLocalDateTime(controlFor(book.id)?.changed_at)}` : book.control_state}>
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
                {#if book.tail_hedge_metrics}
                  <!-- ADR-0012: convexity metrics replace win-rate/expectancy for the tail-hedge sleeve -->
                  <td class="px-3 py-2 text-right tabular-nums" colspan="2"
                      title="ADR-0012: judged on convexity, never expectancy — bleed rate (avg monthly cost, % of basis) · stress-episode payoff (P&L during VIX≥25 or ≥5% SPY drawdown episodes) · portfolio contribution (lab-wide max-drawdown delta with vs without the sleeve)">
                    <span data-testid="tail-hedge-bleed-{book.id}"
                          class={(book.tail_hedge_metrics.bleed_rate_pct_per_month ?? 0) < 0 ? 'text-ctp-red' : 'text-ctp-text'}>
                      bleed {fmtBleed(book.tail_hedge_metrics.bleed_rate_pct_per_month)}
                    </span>
                    <span class="text-ctp-overlay0 mx-1">·</span>
                    <span data-testid="tail-hedge-stress-{book.id}"
                          class={book.tail_hedge_metrics.stress_episode_status === 'no_episode_yet' ? 'text-ctp-overlay0 italic' : 'text-ctp-text'}>
                      stress {fmtStress(book.tail_hedge_metrics)}
                    </span>
                    <span class="text-ctp-overlay0 mx-1">·</span>
                    <span data-testid="tail-hedge-contribution-{book.id}">
                      lab Δdd {fmtContribution(book.tail_hedge_metrics.portfolio_contribution)}
                    </span>
                  </td>
                {:else}
                  <td class="px-3 py-2 text-right">
                    {fmtPct(book.win_rate)}
                    {#if book.win_rate !== null}<span class="text-ctp-overlay0"> (n={book.closed_trades})</span>{/if}
                  </td>
                  <td class="px-3 py-2 text-right tabular-nums" title="expectancy ± 1 standard error, after the $5/contract haircut">
                    {fmtInterval(book.expectancy_after_haircut, book.expectancy_se)}
                  </td>
                {/if}
                <td class="px-3 py-2 text-right text-ctp-red">{book.max_drawdown > 0 ? `-${book.max_drawdown.toFixed(0)}` : '0'}</td>
                <td class="px-3 py-2 text-right">{book.deployed_pct.toFixed(0)}%</td>
                <td class="px-3 py-2 text-right">{book.open_positions}/{book.max_positions}</td>
                <td class="px-3 py-2">
                  <div class="flex flex-wrap gap-1">
                    {#each gateCells(book.live_gate) as cell}
                      <span class="px-1.5 py-0.5 rounded text-[10px] font-bold {gateCellClass[cell.status]}"
                        title={cell.title}>
                        {cell.label}
                      </span>
                    {/each}
                    {#if book.live_gate.eligible}
                      <span class="px-1.5 py-0.5 rounded text-[10px] font-black bg-ctp-green text-ctp-crust">ELIGIBLE</span>
                    {/if}
                  </div>
                  <div class="text-[9px] text-ctp-overlay0 mt-0.5"
                       title="config hash whose era this evidence was accumulated under (#534) — not necessarily the book's current config if it has since resynced">
                    raced:{book.live_gate.as_raced_config_hash.slice(0, 8)}
                    {#if book.live_gate.as_raced_config_hash !== book.config_hash}
                      <span class="text-ctp-yellow font-bold"
                            title="book's current config_hash differs from the era this evidence raced under — a promotion of the CURRENT config cannot cite this evidence">
                        ≠ current
                      </span>
                    {/if}
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="text-[10px] text-ctp-overlay0 mt-2">
        * mean realized P&L per closed trade after the $5/contract slippage haircut (paper fills are optimistic — ADR-0007), ± 1 standard error (n≥2 required; omitted below that).
        The tail-hedge sleeve (ADR-0012) shows bleed rate / stress-episode payoff / lab-wide drawdown contribution instead — it is judged on convexity, never expectancy, and its Live Gate row stays permanently ineligible.
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
        <select bind:value={severityFilter} data-testid="audit-severity-filter"
                class="px-2 py-1 border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text carbon-mono">
          <option value="all">all severities</option>
          <option value="notable-up">hide routine</option>
          <option value="urgent">urgent only</option>
        </select>
        <label class="flex items-center gap-1 text-ctp-subtext0 cursor-pointer">
          <input type="checkbox" bind:checked={groupByType} data-testid="audit-group-by-type" />
          group by type
        </label>
      </div>
    </div>

    {#if filteredEvents.length === 0}
      <div class="carbon-card p-8 text-center text-ctp-overlay0 text-sm" data-testid="audit-empty">
        {#if events.length === 0}
          No audit events match. The executor writes these every run — order lifecycle, control checks, anomalies.
        {:else}
          No events match the current filters.
        {/if}
      </div>
    {:else if groupByType}
      <div class="carbon-card divide-y divide-ctp-surface0/50 text-xs carbon-mono" data-testid="audit-list-grouped">
        {#each groupedEvents as group (group.event_type)}
          <div>
            <button
              class="w-full text-left px-4 py-2 transition flex flex-wrap items-baseline gap-x-3
                {group.severity === 'urgent' ? 'bg-ctp-red/10 hover:bg-ctp-red/15' : 'hover:bg-ctp-surface0/30'}"
              onclick={() => toggleGroup(group.event_type)}
              data-testid="audit-group-{group.event_type}"
            >
              <span class="text-ctp-overlay0 w-3">{expandedGroups.has(group.event_type) ? '▾' : '▸'}</span>
              {#if group.severity === 'urgent'}<span class="text-ctp-red font-black" title="urgent — needs a human">⚠</span>{/if}
              <span class="font-bold {severityCls[group.severity]}">{group.event_type}</span>
              <span class="text-ctp-overlay0">× {group.events.length}</span>
              <span class="text-ctp-overlay0 whitespace-nowrap">latest {formatLocalDateTime(group.latest)}</span>
            </button>
            {#if expandedGroups.has(group.event_type)}
              <div class="divide-y divide-ctp-surface0/30 border-t border-ctp-surface0/50">
                {#each group.events as ev (ev.id)}
                  <button
                    class="w-full text-left px-4 py-2 pl-9 transition flex flex-wrap items-baseline gap-x-3 hover:bg-ctp-surface0/30"
                    onclick={() => (expandedEvent = expandedEvent === ev.id ? null : ev.id)}
                  >
                    <span class="text-ctp-overlay0 whitespace-nowrap">{formatLocalDateTime(ev.run_at)}</span>
                    {#if ev.book_id}
                      <span class="text-ctp-mauve" title={ev.book_id}>{ev.book_label ?? ev.book_id}</span>
                    {/if}
                    <span class="text-ctp-overlay0">by {ev.actor}</span>
                    {#if expandedEvent === ev.id}
                      <pre class="w-full mt-1 p-2 rounded bg-ctp-crust text-ctp-subtext0 overflow-x-auto text-[10px]">{JSON.stringify(ev.payload, null, 2)}</pre>
                    {/if}
                  </button>
                {/each}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <div class="carbon-card divide-y divide-ctp-surface0/50 text-xs carbon-mono" data-testid="audit-list">
        {#each filteredEvents as ev (ev.id)}
          {@const sev = severityOf(ev)}
          <button
            class="w-full text-left px-4 py-2 transition flex flex-wrap items-baseline gap-x-3
              {sev === 'urgent' ? 'bg-ctp-red/10 hover:bg-ctp-red/15' : 'hover:bg-ctp-surface0/30'}
              {sev === 'routine' ? 'opacity-60' : ''}"
            onclick={() => (expandedEvent = expandedEvent === ev.id ? null : ev.id)}
          >
            <span class="text-ctp-overlay0 whitespace-nowrap">{formatLocalDateTime(ev.run_at)}</span>
            {#if sev === 'urgent'}<span class="text-ctp-red font-black" title="urgent — needs a human">⚠</span>{/if}
            <span class="font-bold {severityCls[sev]}">
              {ev.event_type}
            </span>
            {#if ev.book_id}
              <span class="text-ctp-mauve" title={ev.book_id}>{ev.book_label ?? ev.book_id}</span>
            {/if}
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
