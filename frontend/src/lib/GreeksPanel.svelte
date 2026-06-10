<script lang="ts">
  import type { PortfolioObservation } from './api';
  import Tooltip from './ui/Tooltip.svelte';
  import Alert from './ui/Alert.svelte';
  import { IconInfo, IconWarning } from './ui/icons';

  let { observation, maxNetDelta, maxNetVega, maxNetGamma, onReducePositions }: {
    observation: PortfolioObservation;
    maxNetDelta: number;
    maxNetVega: number;
    maxNetGamma: number;
    onReducePositions?: () => void;
  } = $props();

  const g = $derived(observation.greeks);
  const isDeltaExceeded  = $derived(Math.abs(g.net_delta) > maxNetDelta);
  const isVegaExceeded   = $derived(Math.abs(g.net_vega)  > maxNetVega);
  const isGammaExceeded  = $derived(Math.abs(g.net_gamma) > maxNetGamma);
  const anyExceeded      = $derived(isDeltaExceeded || isVegaExceeded || isGammaExceeded);

  const breachCard = 'border-ctp-red glow-red animate-pulse';
  const normalCard = '';
</script>

{#if anyExceeded}
  <div class="mb-6">
    <Alert
      level="critical"
      title="Portfolio Greek limit exceeded"
      message="One or more net Greeks are over their configured limit. Reduce exposure by closing or rolling an open position."
      action={onReducePositions ? { label: 'Review positions →', onclick: onReducePositions } : undefined}
    />
  </div>
{/if}

<section class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
  <div class="carbon-card p-5 transition {isDeltaExceeded ? breachCard : normalCard}">
    <Tooltip text="Sensitivity to $1 move in the underlying. Limit: ±{maxNetDelta}">
      <span class="text-xs font-semibold text-ctp-overlay0 uppercase tracking-wider mb-1 cursor-default flex items-center gap-1">
        Net Delta (Δ) <IconInfo size={11} class="opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl block font-bold text-ctp-text carbon-mono">{g.net_delta.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isDeltaExceeded ? 'text-ctp-red font-bold animate-pulse' : 'text-ctp-overlay0'}">
      {#if isDeltaExceeded}<IconWarning size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetDelta}{/if}
    </span>
  </div>

  <div class="carbon-card p-5">
    <Tooltip text="Daily time-decay P&L. Positive means you earn as time passes.">
      <span class="text-xs font-semibold text-ctp-overlay0 uppercase tracking-wider mb-1 cursor-default flex items-center gap-1">
        Net Theta (Θ) <IconInfo size={11} class="opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl block font-bold text-ctp-text carbon-mono">{g.net_theta.toFixed(2)}</span>
    <span class="text-xs text-ctp-overlay0 block mt-1">Daily decay reward</span>
  </div>

  <div class="carbon-card p-5 transition {isVegaExceeded ? breachCard : normalCard}">
    <Tooltip text="Sensitivity to 1% change in implied volatility. Limit: ±{maxNetVega}">
      <span class="text-xs font-semibold text-ctp-overlay0 uppercase tracking-wider mb-1 cursor-default flex items-center gap-1">
        Net Vega (V) <IconInfo size={11} class="opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl block font-bold text-ctp-text carbon-mono">{g.net_vega.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isVegaExceeded ? 'text-ctp-red font-bold animate-pulse' : 'text-ctp-overlay0'}">
      {#if isVegaExceeded}<IconWarning size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetVega}{/if}
    </span>
  </div>

  <div class="carbon-card p-5 transition {isGammaExceeded ? breachCard : normalCard}">
    <Tooltip text="Rate of delta change per $1 move. High gamma = rapidly shifting exposure. Limit: ±{maxNetGamma}">
      <span class="text-xs font-semibold text-ctp-overlay0 uppercase tracking-wider mb-1 cursor-default flex items-center gap-1">
        Net Gamma (Γ) <IconInfo size={11} class="opacity-50" />
      </span>
    </Tooltip>
    <span class="text-2xl block font-bold text-ctp-text carbon-mono">{g.net_gamma.toFixed(2)}</span>
    <span class="text-xs block mt-1 {isGammaExceeded ? 'text-ctp-red font-bold animate-pulse' : 'text-ctp-overlay0'}">
      {#if isGammaExceeded}<IconWarning size={11} class="inline mr-0.5" /> Limit exceeded{:else}Limit: ±{maxNetGamma}{/if}
    </span>
  </div>
</section>
