<script lang="ts">
  import { onMount } from 'svelte';
  import { getAttention, type AttentionResponse, type AttentionRowItem } from './api';
  import { startPolling } from './poll';
  import { formatLocalDateTime } from './formatters';
  import { toast } from './ui/snackbar.svelte.ts';
  import { IconSuccess, IconWarning } from './ui/icons';
  import Collapsible from './ui/Collapsible.svelte';
  import AttentionItem from './AttentionItem.svelte';

  let {
    onClosePosition,
    onNavigate,
    fleetNav = null,
    openPositionCount = null,
  }: {
    onClosePosition?: (positionId: string) => void;
    onNavigate?: (tab: string, anchor?: string) => void;
    // DESIGN-890 §2: on mobile the Fleet NAV / Open Positions stat cards
    // COLLAPSE into this block's header subtitle; the full cards stay
    // desktop-only in App.svelte. Null while their fetches are pending —
    // the subtitle simply doesn't render (#861: no fabricated values).
    fleetNav?: string | null;
    openPositionCount?: number | null;
  } = $props();

  let attention   = $state<AttentionResponse | null>(null);
  let loadFailed  = $state(false);

  onMount(() => {
    load();
    // Same cadence as every other live panel (StatusStrip, ReconciliationPanel,
    // BooksTab) — a console left open shouldn't show a page-load snapshot.
    startPolling(() => load({ silent: true }));
  });

  async function load(opts: { silent?: boolean } = {}) {
    try {
      attention  = await getAttention();
      loadFailed = false;
    } catch (e: unknown) {
      // Background polls fail silently (matches StatusStrip): a transient
      // blip shouldn't spam toasts every interval or blank a working block.
      if (attention === null) loadFailed = true;
      if (!opts.silent) toast('Failed to load attention: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  }

  // Every list the endpoint returns collapses into one row shape — the
  // action.kind on each item already tells the row what its button does
  // (DESIGN-890.md §1/§3); this just flattens the typed buckets into rows.
  function normalize(a: AttentionResponse): AttentionRowItem[] {
    const items: AttentionRowItem[] = [];

    for (const h of a.halts) {
      items.push({
        id: `halt:${h.scope}`,
        title: `${h.scope_label} — ${h.state.replace(/_/g, ' ')}`,
        detail: h.reason,
        meta: `${h.actor} · ${formatLocalDateTime(h.since)}`,
        action: h.action,
      });
    }

    for (const p of a.p1_actions) {
      items.push({
        id: `p1:${p.position_id}`,
        title: `${p.underlying} ${p.strategy_type.replace(/_/g, ' ')} — ${p.priority}`,
        detail: p.reason,
        meta: p.book_id,
        action: p.action,
      });
    }

    const driftAction = a.reconciliation_drift?.action;
    if (a.reconciliation_drift && driftAction) {
      const r = a.reconciliation_drift;
      items.push({
        id: `recon:${r.run_id}`,
        title: `Reconciliation drift — ${r.drift_count} item${r.drift_count === 1 ? '' : 's'}`,
        detail: r.drift_summary.join('; '),
        meta: formatLocalDateTime(r.run_at),
        action: driftAction,
      });
    }

    for (const o of a.partial_orders) {
      items.push({
        id: `partial:${o.order_ref}`,
        title: `Partial order — ${o.label}`,
        meta: o.book_id,
        action: o.action,
      });
    }

    for (const f of a.flex_discrepancies) {
      items.push({
        id: `flex:${f.exec_id ?? f.description}`,
        title: 'Flex discrepancy',
        detail: f.description,
        action: f.action,
      });
    }

    for (const g of a.delivery_gaps) {
      items.push({
        id: `gap:${g.kind}`,
        title: g.kind === 'digest' ? 'Digest undelivered' : 'Urgent push undelivered',
        meta: g.since ? formatLocalDateTime(g.since) : undefined,
        action: g.action,
      });
    }

    for (const b of a.broker_errors) {
      items.push({
        id: `broker:${b.book_id ?? 'run'}:${b.at}`,
        title: b.book_id ? `${b.book_id} — broker error` : 'Broker error',
        detail: b.instruction,
        meta: formatLocalDateTime(b.at),
        action: b.action,
      });
    }

    for (const u of a.unresolved_urgent_events) {
      items.push({
        id: `urgent:${u.id}`,
        title: u.book_label ? `${u.book_label} — ${u.event_type}` : u.event_type,
        detail: u.detail,
        meta: formatLocalDateTime(u.run_at),
        action: u.action,
      });
    }

    return items;
  }

  const items = $derived(attention ? normalize(attention) : []);
  // problem_count (server-computed, DESIGN-890.md §1) and this filter agree
  // by construction: both exclude exactly action.kind === 'acknowledge_only'.
  const actionable    = $derived(items.filter(i => i.action.kind !== 'acknowledge_only'));
  const informational = $derived(items.filter(i => i.action.kind === 'acknowledge_only'));
</script>

<section class="mb-6" data-testid="attention-block">
  {#if loadFailed}
    <div class="carbon-card p-4 text-xs font-bold text-ctp-red" data-testid="attention-error">
      Failed to load attention — check backend.
    </div>
  {:else if !attention}
    <div class="carbon-card p-6 text-center text-ctp-overlay0 text-sm animate-pulse">
      Checking for anything that needs you…
    </div>
  {:else if attention.status === 'ok'}
    <div class="carbon-card p-4" data-testid="attention-all-clear">
      <div class="flex items-center gap-2 font-bold text-ctp-green">
        <IconSuccess size={16} strokeWidth={2} />
        {attention.headline}
      </div>
      {#if fleetNav !== null || openPositionCount !== null}
        <div class="md:hidden mt-1 text-xs text-ctp-overlay0" data-testid="attention-nav-subtitle">
          {[fleetNav, openPositionCount !== null ? `${openPositionCount} open` : null].filter(Boolean).join(' · ')}
        </div>
      {/if}
    </div>
  {:else}
    <div class="carbon-card overflow-hidden" data-testid="attention-problems">
      <div class="p-4 border-b border-ctp-surface0" data-testid="attention-headline">
        <div class="flex items-center gap-2 font-bold text-ctp-red">
          <IconWarning size={16} strokeWidth={2} />
          {attention.headline}
        </div>
        {#if fleetNav !== null || openPositionCount !== null}
          <div class="md:hidden mt-1 text-xs text-ctp-overlay0" data-testid="attention-nav-subtitle">
            {[fleetNav, openPositionCount !== null ? `${openPositionCount} open` : null].filter(Boolean).join(' · ')}
          </div>
        {/if}
      </div>

      <div class="divide-y divide-ctp-surface0" data-testid="attention-actionable-rows">
        {#each actionable as item (item.id)}
          <AttentionItem {item} {onClosePosition} {onNavigate} onResolved={load} />
        {/each}
      </div>

      {#if informational.length > 0}
        <div class="border-t border-ctp-surface0">
          <Collapsible title="Informational" count={informational.length}>
            <div class="divide-y divide-ctp-surface0" data-testid="attention-informational-rows">
              {#each informational as item (item.id)}
                <AttentionItem {item} informational {onNavigate} onResolved={load} />
              {/each}
            </div>
          </Collapsible>
        </div>
      {/if}
    </div>
  {/if}
</section>
