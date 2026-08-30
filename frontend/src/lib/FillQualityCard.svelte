<script lang="ts">
  import { onMount } from 'svelte';
  import { getFillQuality, type FillQualityReport } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  let report = $state<FillQualityReport | null>(null);

  onMount(async () => {
    try {
      report = await getFillQuality();
    } catch (e: unknown) {
      toast('Failed to load fill quality: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  });

  const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? '—' : v.toFixed(digits));
  const slipCls = (v: number | null | undefined, threshold: number) =>
    v === null || v === undefined ? 'text-ctp-overlay0' : v > threshold ? 'text-ctp-red' : 'text-ctp-green';
</script>

<section>
  <div class="flex items-baseline justify-between mb-4">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Fill Quality</h2>
    <p class="text-xs text-ctp-overlay0">Measured slippage vs the decided mid — the $5/contract haircut is the assumption to beat</p>
  </div>

  {#if !report}
    <div class="carbon-card p-8 text-center text-ctp-overlay0">Loading fill quality…</div>
  {:else if report.rows.length === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-ctp-subtext0 font-medium">No filled orders yet.</p>
      <p class="text-ctp-overlay0 text-xs mt-1">
        Rows appear after the executor's entries fill and the nightly run backfills the fills ledger.
      </p>
    </div>
  {:else}
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
      <div class="carbon-card p-4">
        <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">Orders measured</span>
        <span class="block text-xl font-bold carbon-mono text-ctp-text">{report.orders_analyzed}</span>
        {#if report.orders_awaiting_fills > 0}
          <span class="text-xs text-ctp-yellow">{report.orders_awaiting_fills} awaiting fills ledger</span>
        {/if}
      </div>
      <div class="carbon-card p-4">
        <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">Avg slippage / contract</span>
        <span class="block text-xl font-bold carbon-mono {slipCls(report.avg_slippage_per_contract, report.haircut_per_contract)}">
          {report.avg_slippage_per_contract === null ? '—' : `$${fmt(report.avg_slippage_per_contract)}`}
        </span>
        <span class="text-xs text-ctp-overlay0">haircut assumes ${fmt(report.haircut_per_contract)}</span>
      </div>
      <div class="carbon-card p-4">
        <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">Commissions ledgered</span>
        <span class="block text-xl font-bold carbon-mono text-ctp-text">${fmt(report.total_commissions)}</span>
      </div>
      <div class="carbon-card p-4">
        <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">By action</span>
        <div class="space-y-0.5 mt-1">
          {#each report.by_action as a (a.label)}
            <div class="flex justify-between text-xs carbon-mono">
              <span class="text-ctp-subtext0">{a.label} ×{a.orders}</span>
              <span class={slipCls(a.avg_slippage_per_contract, report.haircut_per_contract)}>
                {a.avg_slippage_per_contract === null ? '—' : `$${fmt(a.avg_slippage_per_contract)}`}
              </span>
            </div>
          {/each}
        </div>
      </div>
    </div>

    <!-- < 768px: one card per order, same fields as the table row (#890 step 7) -->
    <div class="md:hidden space-y-2" data-testid="fill-quality-cards">
      {#each report.rows as r (r.order_ref)}
        <div class="carbon-card p-3 text-xs carbon-mono space-y-1.5">
          <div class="flex items-center justify-between">
            <span class="font-bold text-ctp-text">{r.order_ref}</span>
            <span class="text-ctp-subtext0">{r.action} × {r.contracts}</span>
          </div>
          <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-ctp-overlay0">
            <span>Mid <span class="text-ctp-text">{fmt(r.decision_midpoint)}</span></span>
            <span>Limit <span class="text-ctp-text">{fmt(r.limit_price)}</span></span>
            <span>Filled <span class="text-ctp-text">{r.net_fill_per_share === null ? 'awaiting' : fmt(r.net_fill_per_share)}</span></span>
            <span>Comm <span class="text-ctp-text">{fmt(r.commissions)}</span></span>
            <span title="limit − mid: the rung concession we chose">Chosen <span class={slipCls(r.ladder_concession_per_share, 0)}>{fmt(r.ladder_concession_per_share)}</span></span>
            <span title="fill − limit: what the market moved on top">Market <span class={slipCls(r.market_slippage_per_share, 0)}>{fmt(r.market_slippage_per_share)}</span></span>
          </div>
          <div class="flex items-center justify-between pt-1 border-t border-ctp-surface0">
            <span class="text-ctp-overlay0">Total slip</span>
            <span class="font-bold {slipCls(r.total_slippage_per_share, 0.05)}">{fmt(r.total_slippage_per_share)}</span>
          </div>
        </div>
      {/each}
    </div>

    <div class="hidden md:block carbon-card overflow-x-auto">
      <table class="w-full text-xs carbon-mono" data-testid="fill-quality-table">
        <thead>
          <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
            <th class="px-3 py-2">Order</th>
            <th class="px-3 py-2">Action</th>
            <th class="px-3 py-2 text-right">Qty</th>
            <th class="px-3 py-2 text-right">Decided mid</th>
            <th class="px-3 py-2 text-right">Limit</th>
            <th class="px-3 py-2 text-right">Filled</th>
            <th class="px-3 py-2 text-right" title="limit − mid: the rung concession we chose">Chosen</th>
            <th class="px-3 py-2 text-right" title="fill − limit: what the market moved on top">Market</th>
            <th class="px-3 py-2 text-right">Total slip</th>
            <th class="px-3 py-2 text-right">Comm</th>
          </tr>
        </thead>
        <tbody>
          {#each report.rows as r (r.order_ref)}
            <tr class="border-b border-ctp-surface0/50 text-ctp-text">
              <td class="px-3 py-1.5">{r.order_ref}</td>
              <td class="px-3 py-1.5">{r.action}</td>
              <td class="px-3 py-1.5 text-right">{r.contracts}</td>
              <td class="px-3 py-1.5 text-right">{fmt(r.decision_midpoint)}</td>
              <td class="px-3 py-1.5 text-right">{fmt(r.limit_price)}</td>
              <td class="px-3 py-1.5 text-right">{r.net_fill_per_share === null ? 'awaiting' : fmt(r.net_fill_per_share)}</td>
              <td class="px-3 py-1.5 text-right {slipCls(r.ladder_concession_per_share, 0)}">{fmt(r.ladder_concession_per_share)}</td>
              <td class="px-3 py-1.5 text-right {slipCls(r.market_slippage_per_share, 0)}">{fmt(r.market_slippage_per_share)}</td>
              <td class="px-3 py-1.5 text-right font-bold {slipCls(r.total_slippage_per_share, 0.05)}">{fmt(r.total_slippage_per_share)}</td>
              <td class="px-3 py-1.5 text-right">{fmt(r.commissions)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
