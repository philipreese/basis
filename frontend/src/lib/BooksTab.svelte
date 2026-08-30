<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    getBooks, getAuditEvents, getTradingControl, updateTradingControl,
    getPortfolioObservation, getPortfolioConfig,
    type BookSummary, type AuditEvent, type TradingControlView,
    type PortfolioObservation,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { formatLocalDateTime } from './formatters';
  import { gateCells, gateCellClass, fmtPct, fmtBleed, fmtStress, fmtContribution, fmtInterval } from './bookMetrics';
  import ReconciliationPanel from './ReconciliationPanel.svelte';
  import FlexAuditPanel from './FlexAuditPanel.svelte';
  import LiveOrdersPanel from './LiveOrdersPanel.svelte';
  import BookCard from './BookCard.svelte';
  import { startPolling } from './poll';

  // A resolution correction mutates positions and cash the OVERVIEW renders
  // (#354): without this, a recorded external close leaves the Overview
  // showing the position OPEN with a stale P1 alert whose Close button 400s.
  let { onDataChanged = () => {}, onReducePositions }: {
    onDataChanged?: () => void | Promise<void>;
    // Threaded to B00's BookCard so GreeksPanel's breach alert can send the
    // operator back to Overview's position list.
    onReducePositions?: () => void;
  } = $props();

  let books        = $state<BookSummary[]>([]);
  let events       = $state<AuditEvent[]>([]);
  let control      = $state<TradingControlView | null>(null);
  let isLoading    = $state(true);

  // B00's workbench (#890 step 5): book_summaries() deliberately excludes
  // B00 (it's the manual lane, not a lab book — no Live Gate, no engine
  // variant), so it never appears in `books`. Its BookCard is driven off
  // the same observation Overview used to render GreeksPanel/SafeguardsPanel
  // from, scoped to B00 server-side already (#889).
  let observation  = $state<PortfolioObservation | null>(null);
  let maxNetDelta  = $state(0);
  let maxNetVega   = $state(0);
  let maxNetGamma  = $state(0);
  const manualBook = $derived({
    id: 'B00' as const,
    control_state: control?.controls.find(c => c.scope === 'B00')?.state ?? 'ACTIVE',
  });
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
    // Separate try/catch: B00's workbench figures are additive to the tab —
    // an observation/config hiccup shouldn't blank the Lab Books list above it.
    await loadWorkbench();

    // Live Gate progress, control state, and halt reasons are all
    // page-load snapshots otherwise (#477) — a console left open on Books
    // shows an ACTIVE book through tonight's drift halt forever.
    startPolling(async () => {
      if (controlTarget !== null) return; // don't yank rows out from under an open desktop HALT/RESUME form
      try {
        books = await getBooks();
      } catch {
        /* keep last-known books; background poll failures stay silent */
      }
      await loadControl({ silent: true });
      await loadWorkbench({ silent: true });
    });
  });

  async function loadWorkbench(opts: { silent?: boolean } = {}) {
    try {
      const [obs, cfg] = await Promise.all([getPortfolioObservation(), getPortfolioConfig()]);
      observation = obs;
      maxNetDelta = cfg.portfolio_greek_limits.max_net_delta;
      maxNetVega  = cfg.portfolio_greek_limits.max_net_vega;
      maxNetGamma = cfg.portfolio_greek_limits.max_net_gamma;
    } catch (e: unknown) {
      if (!opts.silent) toast('Failed to load B00 workbench: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

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
  // Desktop-table-only (< 768px renders BookCard's own inline form instead,
  // which never needs the scrollIntoView band-aid below — the form opens
  // inside the tapped card, not at the top of a 9,000px section).
  let controlTarget = $state<{ id: string; toState: 'ACTIVE' | 'HALT_ENTRIES' } | null>(null);
  let controlReason = $state('');
  let controlBusy   = $state(false);

  async function openControl(e: MouseEvent, book: BookSummary) {
    e.stopPropagation(); // the row click filters the audit trail — not this
    controlTarget = { id: book.id, toState: book.control_state === 'ACTIVE' ? 'HALT_ENTRIES' : 'ACTIVE' };
    controlReason = '';
    // #892: the form renders at the top of the section, which for any book
    // below the first screenful is above the viewport — and mobile browsers
    // suppress the input's autofocus, so without this the tap reads as a
    // no-op and a halted book cannot be resumed from a phone at all
    // (ADR-0008 makes the console the only resume surface). Mobile now uses
    // BookCard's inline-per-card form instead, so this only fires from the
    // desktop table.
    await tick();
    const reasonInput = document.querySelector<HTMLInputElement>('[data-testid="book-control-reason"]');
    reasonInput?.scrollIntoView({ block: 'center' });
    reasonInput?.focus();
  }

  async function submitControl(e: SubmitEvent) {
    e.preventDefault();
    if (!controlTarget || controlReason.trim().length < 3) return;
    controlBusy = true;
    try {
      await updateTradingControl(controlTarget.id, controlTarget.toState, controlReason.trim());
      toast(`${controlTarget.id} → ${controlTarget.toState}`, 'success', 4000);
      controlTarget = null;
      await handleControlChanged();
    } catch (err: unknown) {
      toast('Control change failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      controlBusy = false;
    }
  }

  async function handleControlChanged() {
    books = await getBooks();
    await loadControl();
  }
</script>

<div class="space-y-8 mt-2">
  <!-- Drift banner + audited correction tools (#310); a one-liner when clean -->
  <ReconciliationPanel onCorrectionApplied={async () => { books = await getBooks(); await onDataChanged(); }} />

  <!-- Weekly Flex-audit discrepancies awaiting acknowledgment (#571) -->
  <FlexAuditPanel />

  <!-- What's currently resting at the broker (#601) -->
  <LiveOrdersPanel />

  <!-- Manual Book (B00) — #890 step 5: B00 isn't a lab book (book_summaries()
       excludes it, no Live Gate applies to a hand-picked manual position),
       so it gets its own BookCard instead of a row in the table below —
       same halt/resume affordance, plus the Greeks/Safeguards workbench
       relocated from Overview. Shown on every viewport, not just mobile:
       unlike the executor fleet, B00 has no desktop table representation
       to fall back to. -->
  <section>
    <h2 class="text-xl font-bold text-ctp-text tracking-tight mb-4">Manual Book</h2>
    <BookCard
      book={manualBook}
      {control}
      selected={filterBook === 'B00'}
      onSelect={() => selectBook('B00')}
      onControlChanged={handleControlChanged}
      {observation}
      {maxNetDelta}
      {maxNetVega}
      {maxNetGamma}
      {onReducePositions}
    />
  </section>

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
      <!-- < 768px: BookCard grid replaces the table (#890 §2) — each card owns
           its own inline halt/resume form, so there's no shared control state
           and no scroll-to-find-the-form problem the desktop table still has. -->
      <div class="md:hidden space-y-3" data-testid="books-cards">
        {#each books as book (book.id)}
          <BookCard
            {book}
            {control}
            selected={filterBook === book.id}
            onSelect={() => selectBook(book.id)}
            onControlChanged={handleControlChanged}
          />
        {/each}
      </div>

      <div class="hidden md:block carbon-card overflow-x-auto">
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
