<script lang="ts">
  import type { OpportunityRecord } from './api';
  import { computeOverrideStats, filterAndSort, type SortKey, type StatusFilter } from './ledger';
  import { formatDate, formatDollar } from './formatters';
  import Badge from './ui/Badge.svelte';

  let { records }: { records: OpportunityRecord[] } = $props();

  let statusFilter = $state<StatusFilter>('all');
  let sortKey      = $state<SortKey>('date');
  let sortDesc     = $state(true);

  const visible = $derived(filterAndSort(records, statusFilter, sortKey, sortDesc));
  const overrideStats = $derived(computeOverrideStats(records));

  function toggleSort(key: SortKey) {
    if (sortKey === key) sortDesc = !sortDesc;
    else { sortKey = key; sortDesc = true; }
  }

  const filterBtn = (active: boolean) =>
    `px-2.5 py-1 rounded text-xs font-bold uppercase tracking-wider transition ${
      active ? 'bg-ctp-mauve text-ctp-crust' : 'bg-ctp-surface0 text-ctp-subtext0 hover:text-ctp-text'
    }`;
</script>

<section class="mb-8">
  <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
    <h2 class="text-xl font-bold text-ctp-text tracking-tight">Opportunity Ledger</h2>
    {#if records.length > 0}
      <div class="flex items-center gap-1.5" data-testid="ledger-filters">
        {#each (['all', 'accepted', 'bypassed'] as const) as f}
          <button class={filterBtn(statusFilter === f)} onclick={() => (statusFilter = f)}>
            {f}
          </button>
        {/each}
      </div>
    {/if}
  </div>

  {#if records.length === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-ctp-subtext0 text-sm font-semibold">No opportunity records yet.</p>
      <p class="text-ctp-subtext0 text-xs mt-1">Records are created when you save a trade spec or override a suppressed playbook.</p>
    </div>
  {:else}
    {#if overrideStats.bypassed > 0}
      <div class="carbon-card p-4 mb-4 flex flex-wrap items-baseline gap-x-6 gap-y-1 text-xs carbon-mono"
           data-testid="override-summary">
        <span class="font-bold uppercase tracking-wider text-ctp-overlay0">Value of human override</span>
        <span class="text-ctp-subtext0">{overrideStats.bypassed} bypassed · {overrideStats.known} with known outcome</span>
        {#if overrideStats.known > 0}
          <span class="text-ctp-subtext0">{overrideStats.missedWins} would have won</span>
          <span class="font-bold {overrideStats.total <= 0 ? 'text-ctp-green' : 'text-ctp-red'}">
            {overrideStats.total <= 0
              ? `bypassing avoided ${formatDollar(-overrideStats.total)}`
              : `bypassing missed ${formatDollar(overrideStats.total)}`}
            <span class="text-ctp-overlay0 font-normal">(n={overrideStats.known})</span>
          </span>
        {:else}
          <span class="text-ctp-overlay0">no counterfactual outcomes recorded yet</span>
        {/if}
      </div>
    {/if}

    <div class="carbon-card overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-ctp-surface0 bg-ctp-crust">
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Playbook</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Version</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">
                <button class="uppercase tracking-wider hover:text-ctp-text" onclick={() => toggleSort('date')}>
                  Generated {sortKey === 'date' ? (sortDesc ? '↓' : '↑') : ''}
                </button>
              </th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Status</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Bypass Reason</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">
                <button class="uppercase tracking-wider hover:text-ctp-text" onclick={() => toggleSort('outcome')}>
                  Outcome if Taken {sortKey === 'outcome' ? (sortDesc ? '↓' : '↑') : ''}
                </button>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-ctp-surface0">
            {#each visible as record (record.id)}
              <tr class="hover:bg-ctp-surface0/40 transition">
                <td class="px-4 py-3 carbon-mono font-semibold text-ctp-text">{record.playbook_id}</td>
                <td class="px-4 py-3 text-ctp-subtext0">v{record.playbook_version}</td>
                <td class="px-4 py-3 text-ctp-subtext0 whitespace-nowrap">{formatDate(record.generated_at)}</td>
                <td class="px-4 py-3">
                  {#if record.accepted}
                    <Badge variant="success" label="Accepted" />
                  {:else}
                    <Badge variant="warning" label="Bypassed" />
                  {/if}
                </td>
                <td class="px-4 py-3 text-ctp-subtext0 max-w-md whitespace-normal break-words leading-relaxed">
                  {record.bypass_reason ?? '—'}
                </td>
                <td class="px-4 py-3 text-right carbon-mono whitespace-nowrap">
                  {#if record.outcome_if_taken !== null && record.outcome_if_taken !== undefined}
                    <span class="{record.outcome_if_taken >= 0 ? 'text-ctp-green' : 'text-ctp-red'} font-bold">
                      {record.outcome_if_taken >= 0 ? '+' : ''}{formatDollar(record.outcome_if_taken)}
                    </span>
                  {:else}
                    <span class="text-ctp-subtext0">—</span>
                  {/if}
                </td>
              </tr>
            {:else}
              <tr>
                <td colspan="6" class="px-4 py-6 text-center text-ctp-overlay0 text-xs">
                  No {statusFilter} records.
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</section>
