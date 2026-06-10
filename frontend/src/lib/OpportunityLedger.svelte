<script lang="ts">
  import type { OpportunityRecord } from './api';
  import { formatDate, formatDollar } from './formatters';

  let { records }: { records: OpportunityRecord[] } = $props();
</script>

<section class="mb-8">
  <h2 class="text-xl font-bold dark:text-white tracking-tight mb-5">Opportunity Ledger</h2>

  {#if records.length === 0}
    <div class="bg-white dark:bg-slate-900 p-8 rounded-3xl border border-slate-200 dark:border-slate-800 text-center">
      <p class="text-slate-500 text-sm font-semibold">No opportunity records yet.</p>
      <p class="text-slate-400 text-xs mt-1">Records are created when you save a trade spec or override a suppressed playbook.</p>
    </div>
  {:else}
    <div class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 overflow-hidden shadow-sm">
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/80">
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Playbook</th>
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Version</th>
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Generated</th>
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Status</th>
              <th class="text-left px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Bypass Reason</th>
              <th class="text-right px-4 py-3 font-bold text-slate-500 uppercase tracking-wider">Outcome if Taken</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 dark:divide-slate-800">
            {#each records as record (record.id)}
              <tr class="hover:bg-slate-50 dark:hover:bg-slate-900/50 transition">
                <td class="px-4 py-3 font-mono font-semibold dark:text-white">{record.playbook_id}</td>
                <td class="px-4 py-3 text-slate-500">v{record.playbook_version}</td>
                <td class="px-4 py-3 text-slate-500">{formatDate(record.generated_at)}</td>
                <td class="px-4 py-3">
                  {#if record.accepted}
                    <span class="px-2 py-0.5 rounded-full text-xs font-black bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-400 uppercase">Accepted</span>
                  {:else}
                    <span class="px-2 py-0.5 rounded-full text-xs font-black bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-400 uppercase">Bypassed</span>
                  {/if}
                </td>
                <td class="px-4 py-3 text-slate-500 max-w-xs truncate">{record.bypass_reason ?? '—'}</td>
                <td class="px-4 py-3 text-right font-mono">
                  {#if record.outcome_if_taken !== null && record.outcome_if_taken !== undefined}
                    <span class="{record.outcome_if_taken >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'} font-bold">
                      {record.outcome_if_taken >= 0 ? '+' : ''}{formatDollar(record.outcome_if_taken)}
                    </span>
                  {:else}
                    <span class="text-slate-400">—</span>
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
