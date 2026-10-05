<script lang="ts">
  import type { BookSummary } from './api';
  import { checklistMet, checklistRows, fmtHoldings, fmtStage1, gateCellClass, yardstickState } from './bookMetrics';

  // #1132: a share book (book_kind === 'share') is judged by its own
  // yardstick and the ADR-0006 stage-1 bar — never the options Live Gate.
  // Both render as checklists with their detail in text (phones have no
  // tooltips), and every missing-data case says so explicitly instead of
  // falling back to the options chips.
  let { book }: { book: BookSummary } = $props();

  const yState = $derived(yardstickState(book));
  const yardstickRows = $derived(book.trend_yardstick ? checklistRows(book.trend_yardstick.conditions) : []);
  const stage1Rows = $derived(checklistRows(book.stage1_entry_bar.conditions));
  const stage1Met = $derived(checklistMet(book.stage1_entry_bar.conditions));

  const markClass: Record<string, string> = {
    ok: 'text-ctp-green',
    fail: 'text-ctp-overlay0',
    pending: 'text-ctp-yellow',
    nodata: 'text-ctp-overlay0',
  };
</script>

<div class="space-y-2 text-[10px]" data-testid="share-panel-{book.id}">
  <div class="text-ctp-subtext0 tabular-nums" data-testid="share-holdings-{book.id}">
    <span class="font-bold text-ctp-overlay1">Holdings</span> · {fmtHoldings(book.share_holdings)}
  </div>

  <section data-testid="share-yardstick-{book.id}">
    <div class="font-bold text-ctp-overlay1">
      Yardstick{book.trend_yardstick ? ` — ${checklistMet(book.trend_yardstick.conditions)} of ${yardstickRows.length} met` : ''}
      {#if book.trend_yardstick?.ok}
        <span class="ml-1 px-1.5 py-0.5 rounded font-black bg-ctp-green text-ctp-crust">YARDSTICK MET</span>
      {/if}
    </div>
    {#if yState === 'none'}
      <p class="text-ctp-overlay0 italic" data-testid="share-yardstick-none-{book.id}">
        No yardstick of its own yet. This share book cannot be promoted until one is designed and ratified.
      </p>
    {:else if yState === 'waiting'}
      <p class="text-ctp-overlay0 italic" data-testid="share-yardstick-waiting-{book.id}">
        Waiting for first fill. The six-month clock opens at the first fill (evidence era since {book.trend_yardstick?.window_start}).
      </p>
      <div class="flex flex-wrap gap-1 mt-1">
        {#each yardstickRows as row (row.key)}
          <span class="px-1.5 py-0.5 rounded font-bold {gateCellClass.nodata}" title={row.detail}>{row.label}</span>
        {/each}
      </div>
    {:else}
      <ul class="space-y-0.5 mt-0.5">
        {#each yardstickRows as row (row.key)}
          <li class="flex gap-1.5">
            <span class="font-bold {markClass[row.status]}">{row.mark}</span>
            <span><span class="font-bold text-ctp-text">{row.label}</span> <span class="text-ctp-overlay0">{row.detail}</span></span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section data-testid="share-stage1-{book.id}">
    <div class="font-bold text-ctp-overlay1">Stage 1 entry bar — {stage1Met} of {stage1Rows.length} met</div>
    <ul class="space-y-0.5 mt-0.5">
      {#each stage1Rows as row (row.key)}
        <li class="flex gap-1.5">
          <span class="font-bold {markClass[row.status]}">{row.mark}</span>
          <span><span class="font-bold text-ctp-text">{row.label}</span> <span class="text-ctp-overlay0">{row.detail}</span></span>
        </li>
      {/each}
    </ul>
    <div class="text-ctp-overlay0 tabular-nums mt-0.5">{fmtStage1(book.stage1_entry_bar)}</div>
  </section>
</div>
