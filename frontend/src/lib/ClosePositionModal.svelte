<script lang="ts">
  import type { ClosePositionRequest } from './api';

  let {
    positionId,
    onConfirm,
    onCancel,
  }: {
    positionId: string;
    onConfirm: (positionId: string, req: ClosePositionRequest) => Promise<void>;
    onCancel: () => void;
  } = $props();

  let currentValueStr = $state('');
  let exitTrigger = $state<ClosePositionRequest['exit_trigger'] | ''>('');
  let actualMoveStr = $state('');
  let lessonTagsStr = $state('');
  let isSubmitting = $state(false);
  let error = $state('');

  const isValid = $derived(
    currentValueStr.trim() !== '' &&
    !isNaN(parseFloat(currentValueStr)) &&
    exitTrigger !== '' &&
    actualMoveStr.trim() !== '' &&
    !isNaN(parseFloat(actualMoveStr))
  );

  async function handleSubmit() {
    if (!isValid) return;
    isSubmitting = true;
    error = '';
    const lessonTags = lessonTagsStr.split(',').map(t => t.trim()).filter(Boolean);
    try {
      await onConfirm(positionId, {
        current_value_per_share: parseFloat(currentValueStr),
        exit_trigger: exitTrigger as ClosePositionRequest['exit_trigger'],
        actual_underlying_move_pct: parseFloat(actualMoveStr),
        lesson_tags: lessonTags,
      });
    } catch (e: any) {
      error = e.message ?? 'Failed to close position';
    } finally {
      isSubmitting = false;
    }
  }
</script>

<!-- Backdrop -->
<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
  <div class="bg-white dark:bg-slate-900 rounded-3xl border border-slate-200 dark:border-slate-800 shadow-2xl w-full max-w-md mx-4 p-6">
    <div class="flex justify-between items-center mb-5">
      <h3 class="text-base font-black dark:text-white uppercase tracking-wider">Close Position</h3>
      <button onclick={onCancel} class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 font-semibold text-sm">✕</button>
    </div>

    <p class="text-xs text-slate-500 mb-5 font-mono bg-slate-50 dark:bg-slate-950 rounded-lg px-3 py-2 border border-slate-100 dark:border-slate-800">
      Position: <span class="font-bold dark:text-white">{positionId}</span>
    </p>

    <div class="space-y-4">
      <label class="block text-xs font-semibold text-slate-500">
        Current Value / Share ($) <span class="text-rose-500">*</span>
        <input
          type="number"
          step="0.01"
          bind:value={currentValueStr}
          placeholder="e.g. 12.50"
          class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-950 dark:border-slate-700 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-rose-400"
        />
      </label>

      <label class="block text-xs font-semibold text-slate-500">
        Exit Trigger <span class="text-rose-500">*</span>
        <select
          bind:value={exitTrigger}
          class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-950 dark:border-slate-700 text-sm focus:outline-none focus:ring-2 focus:ring-rose-400"
        >
          <option value="">Select…</option>
          <option value="PROFIT_TARGET">Profit Target</option>
          <option value="LOSS_LIMIT">Loss Limit</option>
          <option value="TIME_RULE">Time Rule</option>
          <option value="CATALYST_RULE">Catalyst Rule</option>
          <option value="MANUAL">Manual</option>
        </select>
      </label>

      <label class="block text-xs font-semibold text-slate-500">
        Actual Underlying Move (%) <span class="text-rose-500">*</span>
        <input
          type="number"
          step="0.1"
          bind:value={actualMoveStr}
          placeholder="e.g. -1.5"
          class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-950 dark:border-slate-700 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-rose-400"
        />
      </label>

      <label class="block text-xs font-semibold text-slate-500">
        Lesson Tags (comma-separated, optional)
        <input
          type="text"
          bind:value={lessonTagsStr}
          placeholder="e.g. held-too-long, iv-crush"
          class="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-slate-950 dark:border-slate-700 text-sm focus:outline-none focus:ring-2 focus:ring-rose-400"
        />
      </label>
    </div>

    {#if error}
      <p class="mt-3 text-xs text-rose-600 dark:text-rose-400 font-semibold">{error}</p>
    {/if}

    <div class="flex justify-between items-center mt-6">
      <button
        onclick={onCancel}
        class="px-4 py-2 text-sm font-semibold rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 cursor-pointer"
      >
        Cancel
      </button>
      <button
        onclick={handleSubmit}
        disabled={!isValid || isSubmitting}
        class="px-6 py-2.5 text-sm font-black rounded-xl bg-rose-600 hover:bg-rose-700 disabled:opacity-40 disabled:cursor-not-allowed text-white cursor-pointer transition uppercase tracking-wider"
      >
        {isSubmitting ? 'Closing…' : 'Confirm Close →'}
      </button>
    </div>
  </div>
</div>
