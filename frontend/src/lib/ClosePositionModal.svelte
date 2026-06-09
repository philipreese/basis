<script lang="ts">
  import type { ClosePositionRequest } from './api';
  import Modal from './ui/Modal.svelte';
  import FormField from './ui/FormField.svelte';
  import Button from './ui/Button.svelte';

  let {
    positionId,
    onConfirm,
    onCancel,
  }: {
    positionId: string;
    onConfirm:  (positionId: string, req: ClosePositionRequest) => Promise<void>;
    onCancel:   () => void;
  } = $props();

  let currentValueStr = $state('');
  let exitTrigger     = $state<ClosePositionRequest['exit_trigger'] | ''>('');
  let actualMoveStr   = $state('');
  let lessonTagsStr   = $state('');
  let isSubmitting    = $state(false);
  let error           = $state('');

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
        current_value_per_share:      parseFloat(currentValueStr),
        exit_trigger:                 exitTrigger as ClosePositionRequest['exit_trigger'],
        actual_underlying_move_pct:   parseFloat(actualMoveStr),
        lesson_tags:                  lessonTags,
      });
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : 'Failed to close position';
    } finally {
      isSubmitting = false;
    }
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-950 text-sm focus:outline-none focus:ring-2 focus:ring-rose-400 dark:text-slate-100';
</script>

<Modal title="Close Position" onclose={onCancel}>
  {#snippet body()}
    <div class="space-y-4">
      <p class="text-xs text-slate-500 bg-slate-50 dark:bg-slate-950 rounded-lg px-3 py-2 border border-slate-100 dark:border-slate-800 carbon-mono">
        Position: <span class="font-bold dark:text-white">{positionId}</span>
      </p>

      <FormField label="Current Value / Share ($)" required>
        <input
          type="number"
          step="0.01"
          bind:value={currentValueStr}
          placeholder="e.g. 12.50"
          class="{inputCls} carbon-mono"
        />
      </FormField>

      <FormField label="Exit Trigger" required>
        <select bind:value={exitTrigger} class={inputCls}>
          <option value="">Select a reason…</option>
          <option value="PROFIT_TARGET">Profit Target hit</option>
          <option value="LOSS_LIMIT">Loss Limit hit</option>
          <option value="TIME_RULE">Time Rule (≤21 DTE)</option>
          <option value="CATALYST_RULE">Catalyst Rule</option>
          <option value="MANUAL">Manual decision</option>
        </select>
      </FormField>

      <FormField label="Actual Underlying Move (%)" required hint="Enter as a decimal, e.g. -1.5 for −1.5%">
        <input
          type="number"
          step="0.1"
          bind:value={actualMoveStr}
          placeholder="e.g. -1.5"
          class="{inputCls} carbon-mono"
        />
      </FormField>

      <FormField label="Lesson Tags" hint="Comma-separated, optional. e.g. held-too-long, iv-crush">
        <input
          type="text"
          bind:value={lessonTagsStr}
          placeholder="held-too-long, iv-crush"
          class={inputCls}
        />
      </FormField>

      {#if error}
        <p class="text-xs text-rose-600 dark:text-rose-400 font-semibold">{error}</p>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button
      variant="danger"
      disabled={!isValid || isSubmitting}
      loading={isSubmitting}
      onclick={handleSubmit}
    >
      {isSubmitting ? 'Closing…' : 'Confirm Close →'}
    </Button>
  {/snippet}
</Modal>
