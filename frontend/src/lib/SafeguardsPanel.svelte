<script lang="ts">
  import type { PortfolioObservation } from './api';

  let { observation }: { observation: PortfolioObservation } = $props();
</script>

{#if observation.safeguards.length > 0}
  <section class="mb-8 space-y-3">
    <h3 class="text-sm font-bold text-slate-400 uppercase tracking-wider">Active Risk Safeguard Alerts</h3>
    {#each observation.safeguards as warn}
      <div class="p-4 rounded-xl border flex items-start gap-3 text-sm {warn.severity === 'CRITICAL' ? 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-950/30 dark:bg-rose-950/20 dark:text-rose-400' : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-950/30 dark:bg-amber-950/20 dark:text-amber-400'}">
        <span class="font-bold">{warn.severity === 'CRITICAL' ? '🛑 CRITICAL' : '⚠️ WARNING'}:</span>
        <span>{warn.message}</span>
      </div>
    {/each}
  </section>
{/if}
