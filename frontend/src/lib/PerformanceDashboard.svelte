<script lang="ts">
  import type { PerformanceDiagnostics } from './api';

  let { diagnostics }: { diagnostics: PerformanceDiagnostics } = $props();

  function fmtPct(v: number | null | undefined): string {
    return v !== null && v !== undefined ? `${(v * 100).toFixed(1)}%` : '—';
  }

  function fmtRatio(v: number | null | undefined): string {
    return v !== null && v !== undefined ? v.toFixed(2) : '—';
  }
</script>

<section class="mb-8">
  <h2 class="text-xl font-bold dark:text-white tracking-tight mb-5">Performance Diagnostics</h2>

  <!-- Benchmarks stub -->
  <div class="mb-5 p-4 rounded-2xl border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/50 text-sm text-slate-500">
    <span class="font-bold text-slate-400 uppercase tracking-wider block mb-1">Benchmarks</span>
    {diagnostics.benchmarks.note}
    {#if diagnostics.benchmarks.spy_cagr !== null && diagnostics.benchmarks.spy_cagr !== undefined}
      · SPY CAGR: {(diagnostics.benchmarks.spy_cagr * 100).toFixed(1)}%
    {/if}
  </div>

  {#if diagnostics.playbook_metrics.length === 0}
    <div class="bg-white dark:bg-slate-900 p-8 rounded-3xl border border-slate-200 dark:border-slate-800 text-center">
      <p class="text-slate-500 text-sm font-semibold">No closed positions yet.</p>
      <p class="text-slate-400 text-xs mt-1">Performance metrics populate once positions are closed with post-mortems.</p>
    </div>
  {:else}
    <div class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 overflow-hidden shadow-sm">
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/80">
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Playbook</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Trades</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Win Rate</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Profit Factor</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Avg RoR</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">CAGR</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Sharpe</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 dark:divide-slate-800">
            {#each diagnostics.playbook_metrics as m (m.playbook_id + m.playbook_version)}
              <tr class="hover:bg-slate-50 dark:hover:bg-slate-900/50 transition">
                <td class="px-4 py-3 font-mono font-semibold dark:text-white">
                  {m.playbook_id}
                  <span class="text-slate-400 font-normal"> v{m.playbook_version}</span>
                </td>
                <td class="px-4 py-3 text-right font-mono dark:text-white">{m.total_trades}</td>
                <td class="px-4 py-3 text-right font-mono {m.win_rate !== null && m.win_rate !== undefined && m.win_rate >= 0.5 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}">{fmtPct(m.win_rate)}</td>
                <td class="px-4 py-3 text-right font-mono dark:text-white">{fmtRatio(m.profit_factor)}</td>
                <td class="px-4 py-3 text-right font-mono dark:text-white">{fmtRatio(m.avg_return_on_risk)}</td>
                <td class="px-4 py-3 text-right text-slate-400">{m.cagr}</td>
                <td class="px-4 py-3 text-right text-slate-400">{m.sharpe}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</section>
