<script lang="ts">
  import {
    updateTradingControl,
    type BookSummary, type TradingControlView, type PortfolioObservation,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { formatLocalDateTime } from './formatters';
  import { gateCells, gateCellClass, fmtPct, fmtBleed, fmtStress, fmtContribution, fmtInterval } from './bookMetrics';
  import GreeksPanel from './GreeksPanel.svelte';
  import SafeguardsPanel from './SafeguardsPanel.svelte';

  // B00 (the manual lane) has no BookSummary row — book_summaries() excludes
  // it deliberately (it carries no Live Gate, no engine_variant/underlying,
  // no expectancy tracking; none of that vocabulary applies to a hand-picked
  // manual book, #890 step 5). The card still needs an id and a control
  // state to drive the shared halt/resume form, so B00 is passed in as this
  // minimal shape instead of a fabricated BookSummary.
  type ManualBook = { id: 'B00'; control_state: BookSummary['control_state'] };

  let {
    book,
    control,
    selected = false,
    onSelect,
    onControlChanged,
    observation = null,
    maxNetDelta = 0,
    maxNetVega = 0,
    maxNetGamma = 0,
    onReducePositions,
  }: {
    book: BookSummary | ManualBook;
    control: TradingControlView | null;
    selected?: boolean;
    onSelect: () => void;
    onControlChanged: () => void | Promise<void>;
    // Workbench detail (#890 step 5) — only rendered for B00, and only once
    // the caller has an observation to show. Undefined/null for every other
    // book, which never reads these props.
    observation?: PortfolioObservation | null;
    maxNetDelta?: number;
    maxNetVega?: number;
    maxNetGamma?: number;
    // Keeps GreeksPanel's breach alert actionable in its new home — without
    // it the panel's "Review positions →" link silently disappears.
    onReducePositions?: () => void;
  } = $props();

  const isManual = $derived(book.id === 'B00');
  // Narrows the union once, for every executor-only field below — the
  // template branches on `execBook` (non-null) rather than re-deriving
  // `isManual` per field, so TS sees a real BookSummary, not the union.
  const execBook = $derived(isManual ? null : (book as BookSummary));
  const halted = $derived(book.control_state !== 'ACTIVE');
  const controlInfo = $derived(control?.controls.find(c => c.scope === book.id) ?? null);
  const cells = $derived(execBook ? gateCells(execBook.live_gate) : []);
  const passCount = $derived(cells.filter(c => c.status === 'ok').length);
  const anyGreekLimitExceeded = $derived(observation
    ? Math.abs(observation.greeks.net_delta) > maxNetDelta
      || Math.abs(observation.greeks.net_vega) > maxNetVega
      || Math.abs(observation.greeks.net_gamma) > maxNetGamma
    : false);

  // Same interaction shape as AttentionItem (#890 §3): one form per card,
  // opens/closes locally, never a shared reason applied to more than one
  // book — moved here from BooksTab's single top-of-tab form so a below-
  // the-fold book's HALT/RESUME no longer needs #892's scrollIntoView
  // band-aid, the form is already inside the card that was tapped.
  let formOpen   = $state(false);
  let reason     = $state('');
  let submitting = $state(false);
  let detailOpen = $state(false);

  function openControl(e: MouseEvent) {
    e.stopPropagation(); // tapping the card body filters the audit trail — not this
    reason = '';
    formOpen = true;
  }

  function cancel(e: MouseEvent) {
    e.stopPropagation();
    formOpen = false;
  }

  async function submit(e: Event) {
    e.preventDefault();
    if (reason.trim().length === 0 || submitting) return;
    const value = reason.trim();
    const toState = halted ? 'ACTIVE' : 'HALT_ENTRIES';
    submitting = true;
    try {
      await updateTradingControl(book.id, toState, value);
      toast(`${book.id} → ${toState}`, 'success', 4000);
      formOpen = false;
      await onControlChanged();
    } catch (err: unknown) {
      toast('Control change failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      submitting = false;
    }
  }

  function toggleDetail(e: MouseEvent) {
    e.stopPropagation();
    detailOpen = !detailOpen;
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<article
  class="carbon-card p-3 space-y-2 cursor-pointer transition {selected ? 'bg-ctp-mauve/10' : ''}"
  onclick={onSelect}
  data-testid="book-card-{book.id}"
>
  <div class="flex items-center justify-between gap-2">
    <div class="flex items-center gap-1.5 min-w-0">
      <span class="font-bold text-ctp-text carbon-mono">{book.id}</span>
      {#if halted}
        <span class="text-ctp-red font-bold"
              title={controlInfo ? `${controlInfo.reason} — by ${controlInfo.actor} · ${formatLocalDateTime(controlInfo.changed_at)}` : book.control_state}>
          ⛔ {book.control_state}
        </span>
      {/if}
    </div>

    {#if formOpen}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <form onsubmit={submit} onclick={(e) => e.stopPropagation()}
            class="flex items-center gap-2 shrink-0" data-testid="book-card-{book.id}-form">
        <!-- svelte-ignore a11y_autofocus -->
        <input type="text" bind:value={reason} autofocus
               placeholder="reason (required)"
               data-testid="book-card-{book.id}-reason"
               class="w-28 px-2 py-1 text-xs border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve" />
        <button type="submit" disabled={reason.trim().length === 0 || submitting}
                data-testid="book-card-{book.id}-confirm"
                class="px-2 py-1 text-xs font-bold rounded bg-ctp-surface0 text-ctp-text disabled:opacity-40">
          Confirm
        </button>
        <button type="button" class="text-xs text-ctp-overlay0 hover:underline" onclick={cancel}>
          Cancel
        </button>
      </form>
    {:else if halted}
      <button class="shrink-0 text-[11px] font-bold text-ctp-green hover:underline"
              data-testid="book-card-{book.id}-action" onclick={openControl}>RESUME</button>
    {:else}
      <button class="shrink-0 text-[11px] font-bold text-ctp-red hover:underline"
              data-testid="book-card-{book.id}-action" onclick={openControl}>HALT</button>
    {/if}
  </div>

  {#if !formOpen}
    {#if halted && controlInfo}
      <div class="text-[10px] text-ctp-overlay0" data-testid="book-halt-reason-{book.id}">
        {controlInfo.reason}
      </div>
    {/if}

    {#if execBook}
      <div class="text-[11px] text-ctp-overlay0">
        {execBook.engine_variant}/{execBook.underlying}
        <span title="config hash v{execBook.config_version}">· {execBook.config_hash.slice(0, 8)}</span>
      </div>

      <div class="flex items-center justify-between gap-2 text-xs">
        <span class="font-bold carbon-mono {execBook.pnl >= 0 ? 'text-ctp-green' : 'text-ctp-red'}">
          {execBook.pnl >= 0 ? '+' : ''}{execBook.pnl.toFixed(0)}
        </span>
        <span class="text-ctp-subtext0">{execBook.open_positions}/{execBook.max_positions} pos</span>
        <button type="button"
                class="px-1.5 py-0.5 rounded text-[10px] font-bold {passCount === cells.length ? 'bg-ctp-green/15 text-ctp-green' : 'bg-ctp-surface0 text-ctp-overlay0'}"
                data-testid="book-card-{book.id}-gate-toggle"
                onclick={toggleDetail}>
          {passCount}/{cells.length} conditions{execBook.live_gate.eligible ? ' · ELIGIBLE' : ''}
        </button>
      </div>

      {#if detailOpen}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pt-2 border-t border-ctp-surface0 space-y-1.5"
             data-testid="book-card-{book.id}-detail" onclick={(e) => e.stopPropagation()}>
          <div class="flex flex-wrap gap-1">
            {#each cells as cell}
              <span class="px-1.5 py-0.5 rounded text-[10px] font-bold {gateCellClass[cell.status]}" title={cell.title}>
                {cell.label}
              </span>
            {/each}
          </div>
          <div class="text-[9px] text-ctp-overlay0"
               title="config hash whose era this evidence was accumulated under (#534) — not necessarily the book's current config if it has since resynced">
            raced:{execBook.live_gate.as_raced_config_hash.slice(0, 8)}
            {#if execBook.live_gate.as_raced_config_hash !== execBook.config_hash}
              <span class="text-ctp-yellow font-bold">≠ current</span>
            {/if}
          </div>
          {#if execBook.tail_hedge_metrics}
            <!-- ADR-0012: convexity metrics replace win-rate/expectancy for the tail-hedge sleeve -->
            <div class="text-[10px]"
                 title="ADR-0012: judged on convexity, never expectancy — bleed rate (avg monthly cost, % of basis) · stress-episode payoff (P&L during VIX≥25 or ≥5% SPY drawdown episodes) · portfolio contribution (lab-wide max-drawdown delta with vs without the sleeve)">
              <span class={(execBook.tail_hedge_metrics.bleed_rate_pct_per_month ?? 0) < 0 ? 'text-ctp-red' : 'text-ctp-text'}>
                bleed {fmtBleed(execBook.tail_hedge_metrics.bleed_rate_pct_per_month)}
              </span>
              <span class="text-ctp-overlay0 mx-1">·</span>
              <span class={execBook.tail_hedge_metrics.stress_episode_status === 'no_episode_yet' ? 'text-ctp-overlay0 italic' : 'text-ctp-text'}>
                stress {fmtStress(execBook.tail_hedge_metrics)}
              </span>
              <span class="text-ctp-overlay0 mx-1">·</span>
              <span>lab Δdd {fmtContribution(execBook.tail_hedge_metrics.portfolio_contribution)}</span>
            </div>
          {:else}
            <div class="text-[10px] text-ctp-subtext0">
              {execBook.closed_trades} trades · {fmtPct(execBook.win_rate)} win
              · exp {fmtInterval(execBook.expectancy_after_haircut, execBook.expectancy_se)}
            </div>
          {/if}
          <div class="text-[10px] text-ctp-overlay0">
            Max DD {execBook.max_drawdown > 0 ? `-${execBook.max_drawdown.toFixed(0)}` : '0'} · Deployed {execBook.deployed_pct.toFixed(0)}%
          </div>
        </div>
      {/if}
    {:else}
      <!-- B00's workbench detail (#890 step 5): the manual lane's own Greeks
           and exposure safeguards, relocated here from Overview now that
           compose_observation scopes both to B00 (#889). Reuses GreeksPanel/
           SafeguardsPanel as-is — same components, new home, no re-derivation
           of the numbers they already compute. -->
      <div class="text-[11px] text-ctp-overlay0">Manual lane — journaled positions only</div>

      <div class="flex items-center justify-between gap-2 text-xs">
        <span class="text-ctp-subtext0">Δ/Θ/V/Γ and exposure safeguards</span>
        <button type="button"
                class="px-1.5 py-0.5 rounded text-[10px] font-bold {anyGreekLimitExceeded ? 'bg-ctp-red/15 text-ctp-red' : 'bg-ctp-surface0 text-ctp-overlay0'}"
                data-testid="book-card-{book.id}-workbench-toggle"
                onclick={toggleDetail}>
          Workbench{anyGreekLimitExceeded ? ' · LIMIT EXCEEDED' : ''}
        </button>
      </div>

      {#if detailOpen}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pt-2 border-t border-ctp-surface0 space-y-1.5"
             data-testid="book-card-{book.id}-detail" onclick={(e) => e.stopPropagation()}>
          {#if observation}
            <GreeksPanel {observation} {maxNetDelta} {maxNetVega} {maxNetGamma} {onReducePositions} />
            <SafeguardsPanel {observation} />
          {:else}
            <p class="text-[10px] text-ctp-overlay0">Loading B00's greeks and safeguards…</p>
          {/if}
        </div>
      {/if}
    {/if}
  {/if}
</article>
