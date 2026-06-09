<script lang="ts">
  import type { ClosurePostMortem } from './api';
  import { formatDollar, formatDate, formatPct } from './formatters';

  let { postMortem }: { postMortem: ClosurePostMortem } = $props();

  const EXIT_LABELS: Record<string, string> = {
    PROFIT_TARGET: 'Profit Target',
    LOSS_LIMIT: 'Loss Limit',
    TIME_RULE: 'Time Rule',
    CATALYST_RULE: 'Catalyst Rule',
    MANUAL: 'Manual',
  };
</script>

<article class="bg-white dark:bg-slate-900 rounded-2xl border shadow-sm overflow-hidden
  {postMortem.outcome === 'WIN' ? 'border-emerald-200 dark:border-emerald-900/40' :
   postMortem.outcome === 'LOSS' ? 'border-rose-200 dark:border-rose-900/40' :
   'border-slate-200 dark:border-slate-800'}">

  <div class="p-5 border-b
    {postMortem.outcome === 'WIN' ? 'bg-emerald-50/40 dark:bg-emerald-950/10 border-emerald-100 dark:border-emerald-900/20' :
     postMortem.outcome === 'LOSS' ? 'bg-rose-50/40 dark:bg-rose-950/10 border-rose-100 dark:border-rose-900/20' :
     'bg-slate-50/50 dark:bg-slate-900/50 border-slate-100 dark:border-slate-800'}">
    <div class="flex justify-between items-start">
      <div>
        <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">
          {postMortem.playbook_id ?? 'Unknown Playbook'}
          {#if postMortem.playbook_version} v{postMortem.playbook_version}{/if}
        </p>
        <p class="text-xs text-slate-500">Closed {formatDate(postMortem.exit_date)}</p>
      </div>
      <span class="px-2.5 py-1 text-xs font-black rounded-lg uppercase tracking-wider
        {postMortem.outcome === 'WIN' ? 'bg-emerald-600 text-white' :
         postMortem.outcome === 'LOSS' ? 'bg-rose-600 text-white' :
         'bg-slate-400 text-white'}">
        {postMortem.outcome}
      </span>
    </div>
  </div>

  <div class="p-5 space-y-4">
    <div class="grid grid-cols-3 gap-3">
      <div>
        <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Realized P&L</span>
        <span class="text-base font-black font-mono {postMortem.realized_pnl >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}">
          {postMortem.realized_pnl >= 0 ? '+' : ''}{formatDollar(postMortem.realized_pnl)}
        </span>
      </div>
      <div>
        <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Actual Move</span>
        <span class="text-base font-black font-mono dark:text-white">
          {postMortem.actual_underlying_move_pct >= 0 ? '+' : ''}{formatPct(postMortem.actual_underlying_move_pct)}
        </span>
      </div>
      <div>
        <span class="text-[10px] font-bold text-slate-400 uppercase block mb-1">Exit Trigger</span>
        <span class="text-sm font-semibold dark:text-white">{EXIT_LABELS[postMortem.exit_trigger] ?? postMortem.exit_trigger}</span>
      </div>
    </div>

    {#if postMortem.lesson_tags.length > 0}
      <div class="flex flex-wrap gap-2">
        {#each postMortem.lesson_tags as tag}
          <span class="px-2 py-0.5 text-[10px] font-semibold rounded-full bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300">{tag}</span>
        {/each}
      </div>
    {/if}

    {#if postMortem.user_override_logged}
      <span class="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-bold rounded-full bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300">
        ⚠ Override logged
      </span>
    {/if}
  </div>
</article>
