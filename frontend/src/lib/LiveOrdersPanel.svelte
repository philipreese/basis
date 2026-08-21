<script lang="ts">
  import { onMount } from 'svelte';
  import { getLiveOrders, type LiveOrder } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { startPolling } from './poll';

  // What the system currently believes is resting at the broker (#601) — so
  // an operator can directly compare against the IBKR app during an
  // incident instead of reconstructing it from audit rows.
  let orders = $state<LiveOrder[]>([]);
  let loaded = $state(false);

  onMount(() => {
    load();
    startPolling(() => load({ silent: true }));
  });

  async function load(opts: { silent?: boolean } = {}) {
    try {
      orders = await getLiveOrders();
    } catch (e: unknown) {
      if (!opts.silent) toast('Failed to load live orders: ' + (e instanceof Error ? e.message : String(e)), 'error');
    } finally {
      loaded = true;
    }
  }

  const statusCls: Record<LiveOrder['status'], string> = {
    STAGED: 'bg-ctp-surface0 text-ctp-overlay0',
    SUBMITTED: 'bg-ctp-blue/15 text-ctp-blue',
    PARTIAL: 'bg-ctp-yellow/15 text-ctp-yellow',
  };
</script>

{#if loaded}
  <section class="carbon-card p-5" data-testid="live-orders-panel">
    <div class="flex items-baseline justify-between mb-3">
      <h2 class="text-base font-bold text-ctp-text tracking-tight">Live at broker</h2>
      <span class="text-xs text-ctp-overlay0">{orders.length} order{orders.length === 1 ? '' : 's'} resting</span>
    </div>
    {#if orders.length === 0}
      <p class="text-xs text-ctp-overlay0" data-testid="live-orders-empty">Nothing resting at the broker right now.</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="w-full text-xs carbon-mono" data-testid="live-orders-table">
          <thead>
            <tr class="text-left text-ctp-overlay0 uppercase tracking-wider border-b border-ctp-surface0">
              <th class="px-3 py-2">Ref</th>
              <th class="px-3 py-2">Book</th>
              <th class="px-3 py-2">Spread</th>
              <th class="px-3 py-2">Type</th>
              <th class="px-3 py-2">TIF</th>
              <th class="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {#each orders as o (o.order_ref)}
              <tr class="border-b border-ctp-surface0/50">
                <td class="px-3 py-2 text-ctp-overlay0" title={o.order_ref}>{o.order_ref}</td>
                <td class="px-3 py-2 font-bold text-ctp-text">{o.book_id}</td>
                <td class="px-3 py-2 text-ctp-mauve">{o.label}</td>
                <td class="px-3 py-2 text-ctp-subtext0">{o.order_type}</td>
                <td class="px-3 py-2 text-ctp-subtext0">{o.tif}</td>
                <td class="px-3 py-2">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-bold {statusCls[o.status]}">{o.status}</span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
{/if}
