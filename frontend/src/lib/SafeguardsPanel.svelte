<script lang="ts">
  import type { PortfolioObservation } from './api';
  import Alert from './ui/Alert.svelte';

  let { observation }: { observation: PortfolioObservation } = $props();
</script>

{#if observation.safeguards.length > 0}
  <section class="mb-8 space-y-3">
    <h3 class="text-xs font-bold text-ctp-subtext0 uppercase tracking-wider">Active Risk Safeguard Alerts</h3>
    {#each observation.safeguards as warn}
      <Alert
        level={warn.severity === 'CRITICAL' ? 'critical' : 'warning'}
        title={warn.severity === 'CRITICAL' ? 'Critical' : 'Warning'}
        message={warn.message}
      />
    {/each}
  </section>
{/if}
