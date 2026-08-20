<script lang="ts">
  import { onMount } from 'svelte';
  import { getRegimeHitRate, type RegimeHitRateReport } from './api';
  import { REGIME_DISPLAY } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  let report = $state<RegimeHitRateReport | null>(null);

  onMount(async () => {
    try {
      report = await getRegimeHitRate();
    } catch (e: unknown) {
      toast('Failed to load regime hit-rate: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  });

  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(2));
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`);
  const pnlCls = (v: number | null | undefined) =>
    v === null || v === undefined ? 'text-ctp-overlay0' : v >= 0 ? 'text-ctp-green' : 'text-ctp-red';
  const regimeLabel = (r: string) => REGIME_DISPLAY[r]?.label ?? r;
</script>

<section>
  <div class="flex items-baseline justify-between mb-4">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Regime Hit-Rate</h2>
    <p class="text-xs text-ctp-overlay0">When the gate said yes, which regimes actually paid? (observational complement to the B12 no-gate control)</p>
  </div>

  {#if !report}
    <div class="carbon-card p-8 text-center text-ctp-overlay0">Loading regime hit-rate…</div>
  {:else if report.closed_trades === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-ctp-subtext0 font-medium">No closed trades yet.</p>
      <p class="text-ctp-overlay0 text-xs mt-1">
        Each closed position's entry-day regime is stamped at entry — outcomes group here as they close.
      </p>
    </div>
  {:else}
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
      <div class="carbon-card overflow-x-auto">
        <table class="w-full text-xs carbon-mono" data-testid="regime-hit-rate-table">
          <thead>
            <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
              <th class="px-3 py-2">Entry regime</th>
              <th class="px-3 py-2 text-right">n</th>
              <th class="px-3 py-2 text-right">Wins</th>
              <th class="px-3 py-2 text-right">Win rate</th>
              <th class="px-3 py-2 text-right">Avg P&L</th>
              <th class="px-3 py-2 text-right">Total</th>
            </tr>
          </thead>
          <tbody>
            {#each report.by_regime as r (r.regime)}
              <tr class="border-b border-ctp-surface0/50 text-ctp-text">
                <td class="px-3 py-1.5 font-bold">{regimeLabel(r.regime)}</td>
                <td class="px-3 py-1.5 text-right">{r.closed_trades}</td>
                <td class="px-3 py-1.5 text-right">{r.wins}</td>
                <td class="px-3 py-1.5 text-right">{pct(r.win_rate)}</td>
                <td class="px-3 py-1.5 text-right {pnlCls(r.avg_pnl)}">{fmt(r.avg_pnl)}</td>
                <td class="px-3 py-1.5 text-right font-bold {pnlCls(r.total_pnl)}">{fmt(r.total_pnl)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <div class="carbon-card overflow-x-auto">
        <table class="w-full text-xs carbon-mono">
          <thead>
            <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
              <th class="px-3 py-2">Engine</th>
              <th class="px-3 py-2">Entry regime</th>
              <th class="px-3 py-2 text-right">n</th>
              <th class="px-3 py-2 text-right">Win rate</th>
              <th class="px-3 py-2 text-right">Avg P&L</th>
            </tr>
          </thead>
          <tbody>
            {#each report.by_engine_regime as r (`${r.engine_variant}:${r.regime}`)}
              <tr class="border-b border-ctp-surface0/50 text-ctp-text">
                <td class="px-3 py-1.5 font-bold">{r.engine_variant}</td>
                <td class="px-3 py-1.5">{regimeLabel(r.regime)}</td>
                <td class="px-3 py-1.5 text-right">{r.closed_trades}</td>
                <td class="px-3 py-1.5 text-right">{pct(r.win_rate)}</td>
                <td class="px-3 py-1.5 text-right {pnlCls(r.avg_pnl)}">{fmt(r.avg_pnl)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</section>
