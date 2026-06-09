<script lang="ts">
  import type { PortfolioObservation } from './api';
  import Tooltip from './ui/Tooltip.svelte';
  import { Info, AlertTriangle } from 'lucide-svelte';

  let { observation, maxNetDelta, maxNetVega, maxNetGamma }: {
    observation: PortfolioObservation;
    maxNetDelta: number;
    maxNetVega: number;
    maxNetGamma: number;
  } = $props();

  const g = $derived(observation.greeks);
  const isDeltaExceeded  = $derived(Math.abs(g.net_delta) > maxNetDelta);
  const isVegaExceeded   = $derived(Math.abs(g.net_vega)  > maxNetVega);
  const isGammaExceeded  = $derived(Math.abs(g.net_gamma) > maxNetGamma);

  const breachCard = 'border-rose-400 dark:border-rose-600 bg-rose-50/10 dark:bg-rose-950/10 dark:glow-rose animate-pulse';
  const normalCard = '';
</script>

<section class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
  <div class="carbon-card p-5 transition shadow-sm {isDeltaExceeded ? breachCard : normalCard}">
    <Tooltip text="Sensitivity to $1 move in the underlying. Limit: ±{maxNetDelta}">
      <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1 cursor-default">
        Net Delta (Δ) <Info size={11} class="inline opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_delta.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isDeltaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-400'}">
      {#if isDeltaExceeded}<AlertTriangle size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetDelta}{/if}
    </span>
  </div>

  <div class="carbon-card p-5 shadow-sm">
    <Tooltip text="Daily time-decay P&L. Positive means you earn as time passes.">
      <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1 cursor-default">
        Net Theta (Θ) <Info size={11} class="inline opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_theta.toFixed(2)}</span>
    <span class="text-xs text-slate-400 block mt-1">Daily decay reward</span>
  </div>

  <div class="carbon-card p-5 transition shadow-sm {isVegaExceeded ? breachCard : normalCard}">
    <Tooltip text="Sensitivity to 1% change in implied volatility. Limit: ±{maxNetVega}">
      <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1 cursor-default">
        Net Vega (V) <Info size={11} class="inline opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_vega.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isVegaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-400'}">
      {#if isVegaExceeded}<AlertTriangle size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetVega}{/if}
    </span>
  </div>

  <div class="carbon-card p-5 transition shadow-sm {isGammaExceeded ? breachCard : normalCard}">
    <Tooltip text="Rate of delta change per $1 move. High gamma = rapidly shifting exposure. Limit: ±{maxNetGamma}">
      <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider block mb-1 cursor-default">
        Net Gamma (Γ) <Info size={11} class="inline opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl font-bold dark:text-white carbon-mono">{g.net_gamma.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isGammaExceeded ? 'text-rose-500 dark:text-rose-400 font-bold animate-pulse' : 'text-slate-400'}">
      {#if isGammaExceeded}<AlertTriangle size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetGamma}{/if}
    </span>
  </div>
</section>
