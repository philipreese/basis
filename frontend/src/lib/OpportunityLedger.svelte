<script lang="ts">
  import type { OpportunityRecord } from './api';
  import { formatDate, formatDollar } from './formatters';
  import Badge from './ui/Badge.svelte';

  let { records }: { records: OpportunityRecord[] } = $props();
</script>

<section class="mb-8">
  <h2 class="text-xl font-bold text-ctp-text tracking-tight mb-5">Opportunity Ledger</h2>

  {#if records.length === 0}
    <div class="carbon-card p-8 text-center">
      <p class="text-ctp-subtext0 text-sm font-semibold">No opportunity records yet.</p>
      <p class="text-ctp-subtext0 text-xs mt-1">Records are created when you save a trade spec or override a suppressed playbook.</p>
    </div>
  {:else}
    <div class="carbon-card overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-ctp-surface0 bg-ctp-crust">
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Playbook</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Version</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Generated</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Status</th>
              <th scope="col" class="text-left px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Bypass Reason</th>
              <th scope="col" class="text-right px-4 py-3 font-bold text-ctp-subtext0 uppercase tracking-wider">Outcome if Taken</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-ctp-surface0">
            {#each records as record (record.id)}
              <tr class="hover:bg-ctp-surface0/40 transition">
                <td class="px-4 py-3 carbon-mono font-semibold text-ctp-text">{record.playbook_id}</td>
                <td class="px-4 py-3 text-ctp-subtext0">v{record.playbook_version}</td>
                <td class="px-4 py-3 text-ctp-subtext0">{formatDate(record.generated_at)}</td>
                <td class="px-4 py-3">
                  {#if record.accepted}
                    <Badge variant="success" label="Accepted" />
                  {:else}
                    <Badge variant="warning" label="Bypassed" />
                  {/if}
                </td>
                <td class="px-4 py-3 text-ctp-subtext0 max-w-xs truncate">{record.bypass_reason ?? '—'}</td>
                <td class="px-4 py-3 text-right carbon-mono">
                  {#if record.outcome_if_taken !== null && record.outcome_if_taken !== undefined}
                    <span class="{record.outcome_if_taken >= 0 ? 'text-ctp-green' : 'text-ctp-red'} font-bold">
                      {record.outcome_if_taken >= 0 ? '+' : ''}{formatDollar(record.outcome_if_taken)}
                    </span>
                  {:else}
                    <span class="text-ctp-subtext0">—</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
</section>
