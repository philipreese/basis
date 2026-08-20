<script lang="ts">
  import { onMount } from 'svelte';
  import { getLeaderboard, type LeaderboardReport } from './api';
  import { toast } from './ui/snackbar.svelte.ts';

  let report = $state<LeaderboardReport | null>(null);

  onMount(async () => {
    try {
      report = await getLeaderboard();
    } catch (e: unknown) {
      toast('Failed to load leaderboard: ' + (e instanceof Error ? e.message : String(e)), 'error');
    }
  });

  const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(2));
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`);
  const expCls = (v: number | null | undefined) =>
    v === null || v === undefined ? 'text-ctp-overlay0' : v >= 0 ? 'text-ctp-green' : 'text-ctp-red';
  const verdictCls = (v: string) =>
    v === 'insufficient data' ? 'bg-ctp-surface0 text-ctp-overlay0'
    : v === 'non-monotonic' ? 'bg-ctp-yellow/15 text-ctp-yellow'
    : 'bg-ctp-green/15 text-ctp-green';
</script>

<section>
  <div class="flex items-baseline justify-between mb-4">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Leaderboard</h2>
    <p class="text-xs text-ctp-overlay0">Expectancy after the $5 haircut and ledgered commissions — the number the Live Gate judges</p>
  </div>

  {#if !report}
    <div class="carbon-card p-8 text-center text-ctp-overlay0">Loading leaderboard…</div>
  {:else}
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
      <!-- Ranked books -->
      <div class="carbon-card overflow-x-auto">
        <table class="w-full text-xs carbon-mono" data-testid="leaderboard-table">
          <thead>
            <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
              <th class="px-3 py-2">#</th>
              <th class="px-3 py-2">Book</th>
              <th class="px-3 py-2">Arm</th>
              <th class="px-3 py-2 text-right">n</th>
              <th class="px-3 py-2 text-right">Win</th>
              <th class="px-3 py-2 text-right">Expectancy</th>
              <th class="px-3 py-2 text-right">P&L</th>
            </tr>
          </thead>
          <tbody>
            {#each report.ranked as s, i (s.id)}
              <tr class="border-b border-ctp-surface0/50 text-ctp-text {s.closed_trades === 0 ? 'opacity-50' : ''}">
                <td class="px-3 py-1.5 text-ctp-overlay0">{i + 1}</td>
                <td class="px-3 py-1.5 font-bold">{s.id}</td>
                <td class="px-3 py-1.5 truncate max-w-40" title={s.name}>{s.name}</td>
                <td class="px-3 py-1.5 text-right">{s.closed_trades}</td>
                <td class="px-3 py-1.5 text-right">{pct(s.win_rate)}</td>
                <td class="px-3 py-1.5 text-right font-bold {expCls(s.expectancy_after_haircut)}">{fmt(s.expectancy_after_haircut)}</td>
                <td class="px-3 py-1.5 text-right {expCls(s.pnl)}">{fmt(s.pnl)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <!-- Knob sweeps -->
      <div class="space-y-3">
        {#each report.sweeps as sweep (sweep.dimension)}
          <div class="carbon-card p-4" data-testid="knob-sweep">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-xs font-bold text-ctp-text uppercase tracking-wider">{sweep.dimension}</h3>
              <span class="px-2 py-0.5 rounded text-xs font-bold {verdictCls(sweep.verdict)}">{sweep.verdict}</span>
            </div>
            <div class="flex flex-wrap gap-2">
              {#each sweep.points as p (p.book_id)}
                <div class="flex-1 min-w-24 bg-ctp-crust rounded px-2 py-1.5 border border-ctp-surface0">
                  <div class="flex justify-between text-xs carbon-mono">
                    <span class="text-ctp-subtext0">{p.book_id}</span>
                    <span class="text-ctp-overlay0">n={p.closed_trades}</span>
                  </div>
                  <div class="text-xs carbon-mono text-ctp-text">{p.knob_value}</div>
                  <div class="text-sm carbon-mono font-bold {expCls(p.expectancy_after_haircut)}">{fmt(p.expectancy_after_haircut)}</div>
                </div>
              {/each}
            </div>
          </div>
        {/each}
        <p class="text-xs text-ctp-overlay0">
          A sweep verdict only speaks once every point has ≥{report.min_trades_per_point} closed trades — anything thinner reads "insufficient data".
        </p>
      </div>
    </div>
  {/if}
</section>
