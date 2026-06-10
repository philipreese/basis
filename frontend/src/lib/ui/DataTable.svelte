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
  <table class="w-full text-sm border-collapse">
    <thead>
      <tr class="bg-ctp-crust border-b border-ctp-surface0">
        {#each columns as col}
          <th scope="col" class="px-4 py-2.5 font-bold text-ctp-overlay0 uppercase tracking-wider {alignClass(col.align)}">
            {col.label}
          </th>
        {/each}
      </tr>
    </thead>
    <tbody class="divide-y divide-ctp-surface0">
      {#if rows.length === 0}
        <tr>
          <td colspan={columns.length} class="px-4 py-6 text-center text-ctp-overlay0">{empty}</td>
        </tr>
      {:else}
        {#each rows as row}
          <tr class="hover:bg-ctp-surface0/50 transition-colors">
            {#each columns as col}
              <td class="px-4 py-2.5 text-ctp-subtext1 {alignClass(col.align)} {col.mono ? 'carbon-mono' : ''}">
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
