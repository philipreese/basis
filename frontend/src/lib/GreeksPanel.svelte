<script lang="ts">
  import type { PortfolioObservation } from './api';

  let { observation, maxNetDelta, maxNetVega, maxNetGamma }: {
    observation: PortfolioObservation;
    maxNetDelta: number;
    maxNetVega: number;
    maxNetGamma: number;
  } = $props();

  const g = $derived(observation.greeks);
  const isDeltaExceeded = $derived(Math.abs(g.net_delta) > maxNetDelta);
  const isVegaExceeded = $derived(Math.abs(g.net_vega) > maxNetVega);
  const isGammaExceeded = $derived(Math.abs(g.net_gamma) > maxNetGamma);
</script>

<section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
  <div class="carbon-card p-5 transition shadow-sm {isDeltaExceeded ? 'border-rose-500 dark:border-rose-700 bg-rose-50/10 dark:bg-rose-950/10 dark:glow-rose animate-pulse' : ''}">
    <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Delta (Δ)</span>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_delta.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isDeltaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-500'}">Limit: ±{maxNetDelta}</span>
  </div>

  <div class="carbon-card p-5 shadow-sm">
    <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Theta (Θ)</span>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_theta.toFixed(2)}</span>
    <span class="text-xs text-slate-500 block mt-1">Daily Theta reward</span>
  </div>

  <div class="carbon-card p-5 transition shadow-sm {isVegaExceeded ? 'border-rose-500 dark:border-rose-700 bg-rose-50/10 dark:bg-rose-950/10 dark:glow-rose animate-pulse' : ''}">
    <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Vega</span>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_vega.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isVegaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-500'}">Limit: ±{maxNetVega}</span>
  </div>

  <div class="carbon-card p-5 transition shadow-sm {isGammaExceeded ? 'border-rose-500 dark:border-rose-700 bg-rose-50/10 dark:bg-rose-950/10 dark:glow-rose animate-pulse' : ''}">
    <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1">Portfolio Net Gamma (Γ)</span>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_gamma.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isGammaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-500'}">Limit: ±{maxNetGamma}</span>
  </div>
</section>
