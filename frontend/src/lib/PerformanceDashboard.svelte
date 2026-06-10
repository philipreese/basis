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
  <h2 class="text-xl font-bold text-ctp-text tracking-tight mb-5">Performance Diagnostics</h2>

  <!-- Benchmarks stub -->
  <div class="mb-5 p-4 rounded-xl border border-ctp-surface0 bg-ctp-surface0/30 text-sm text-ctp-subtext0">
    <span class="font-bold text-ctp-subtext1 uppercase tracking-wider block mb-1">Benchmarks</span>
    {diagnostics.benchmarks.note}
    {#if diagnostics.benchmarks.spy_cagr !== null && diagnostics.benchmarks.spy_cagr !== undefined}
      · SPY CAGR: {(diagnostics.benchmarks.spy_cagr * 100).toFixed(1)}%
    {/if}
  </div>

  {#if diagnostics.playbook_metrics.length === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-ctp-subtext0 text-sm font-semibold">No closed positions yet.</p>
      <p class="text-ctp-subtext0 text-xs mt-1">Performance metrics populate once positions are closed with post-mortems.</p>
    </div>
  {:else}
    <div class="carbon-card overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-ctp-surface0 bg-ctp-crust">
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Playbook</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Trades</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Win Rate</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Profit Factor</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Avg RoR</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">CAGR</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Sharpe</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-ctp-surface0">
            {#each diagnostics.playbook_metrics as m (m.playbook_id + m.playbook_version)}
              <tr class="hover:bg-ctp-surface0/40 transition">
                <td class="px-4 py-3 carbon-mono font-semibold text-ctp-text">
                  {m.playbook_id}
                  <span class="text-ctp-subtext0 font-normal"> v{m.playbook_version}</span>
                </td>
                <td class="px-4 py-3 text-right carbon-mono text-ctp-text">{m.total_trades}</td>
                <td class="px-4 py-3 text-right carbon-mono {m.win_rate !== null && m.win_rate !== undefined && m.win_rate >= 0.5 ? 'text-ctp-green' : 'text-ctp-red'}">{fmtPct(m.win_rate)}</td>
                <td class="px-4 py-3 text-right carbon-mono text-ctp-text">{fmtRatio(m.profit_factor)}</td>
                <td class="px-4 py-3 text-right carbon-mono text-ctp-text">{fmtRatio(m.avg_return_on_risk)}</td>
                <td class="px-4 py-3 text-right text-ctp-subtext0">{m.cagr}</td>
                <td class="px-4 py-3 text-right text-ctp-subtext0">{m.sharpe}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</section>
