<script lang="ts">
  import type { Snippet } from 'svelte';

  type Column = {
    key:    string;
    label:  string;
    align?: 'left' | 'center' | 'right';
    mono?:  boolean;
  };

  let {
    columns,
    rows,
    cell,
    empty = 'No data.',
  }: {
    columns: Column[];
    rows:    Record<string, unknown>[];
    cell?:   Snippet<[{ row: Record<string, unknown>; col: Column }]>;
    empty?:  string;
  } = $props();

  const alignClass = (a?: string) =>
    a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left';
</script>

<div class="overflow-x-auto">
  <table class="w-full text-xs border-collapse">
    <thead>
      <tr class="bg-slate-50 dark:bg-slate-900/80 border-b border-slate-200 dark:border-slate-800">
        {#each columns as col}
          <th class="px-4 py-2.5 font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider {alignClass(col.align)}">
            {col.label}
          </th>
        {/each}
      </tr>
    </thead>
    <tbody class="divide-y divide-slate-100 dark:divide-slate-800/60">
      {#if rows.length === 0}
        <tr>
          <td colspan={columns.length} class="px-4 py-6 text-center text-slate-400 dark:text-slate-500">{empty}</td>
        </tr>
      {:else}
        {#each rows as row}
          <tr class="hover:bg-slate-50 dark:hover:bg-slate-900/40 transition-colors">
            {#each columns as col}
              <td class="px-4 py-2.5 {alignClass(col.align)} {col.mono ? 'carbon-mono' : ''}">
                {#if cell}
                  {@render cell({ row, col })}
                {:else}
                  {row[col.key] ?? '—'}
                {/if}
              </td>
            {/each}
          </tr>
        {/each}
      {/if}
    </tbody>
  </table>
</div>
