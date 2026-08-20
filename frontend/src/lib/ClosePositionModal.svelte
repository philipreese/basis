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

  // Svelte coerces bind:value on type=number inputs to number (or null when
  // empty) — validating these as strings throws and bricks the Confirm button.
  let currentValue    = $state<number | null>(null);
  let exitTrigger     = $state<ClosePositionRequest['exit_trigger'] | ''>('');
  let actualMove      = $state<number | null>(null);
  let lessonTagsStr   = $state('');
  let isSubmitting    = $state(false);
  let error           = $state('');

  const isValid = $derived(
    currentValue !== null && !isNaN(currentValue) &&
    exitTrigger !== '' &&
    actualMove !== null && !isNaN(actualMove)
  );

  async function handleSubmit() {
    if (!isValid || currentValue === null || actualMove === null || exitTrigger === '') return;
    isSubmitting = true;
    error = '';
    const lessonTags = lessonTagsStr.split(',').map(t => t.trim()).filter(Boolean);
    try {
      await onConfirm(positionId, {
        current_value_per_share:       currentValue,
        exit_trigger:                  exitTrigger,
        actual_underlying_move_pct:    actualMove,
        lesson_tags:                   lessonTags,
        acknowledge_broker_divergence: false, // App.svelte escalates on executor books (#279)
        acknowledge_cancelled:         false, // no UI for this yet — tracked in #516
      });
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : 'Failed to close position';
    } finally {
      isSubmitting = false;
    }
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-ctp-surface1 rounded-lg bg-ctp-crust text-ctp-text text-sm focus:outline-none focus:ring-2 focus:ring-ctp-mauve carbon-mono';
</script>

<Modal title="Close Position" onclose={onCancel}>
  {#snippet body()}
    <div class="space-y-4">
      <p class="text-xs text-ctp-subtext0 bg-ctp-crust rounded-lg px-3 py-2 border border-ctp-surface0 carbon-mono">
        Position: <span class="font-bold text-ctp-text">{positionId}</span>
      </p>

      <FormField label="Current Value / Share ($)" required>
        <input type="number" step="0.01" bind:value={currentValue} placeholder="e.g. 12.50" class="{inputCls}" />
      </FormField>

      <FormField label="Exit Trigger" required>
        <select bind:value={exitTrigger} class={inputCls}>
          <option value="">Select a reason…</option>
          <option value="PROFIT_TARGET">Profit Target hit</option>
          <option value="LOSS_LIMIT">Loss Limit hit</option>
          <option value="TIME_RULE">Time Rule (≤21 DTE)</option>
          <option value="CATALYST_RULE">Catalyst Rule</option>
          <option value="REGIME_FLIP">Regime Flip</option>
          <option value="ASSIGNMENT_RISK">Assignment Risk</option>
          <option value="EXPIRY">Expiry</option>
          <option value="MANUAL">Manual decision</option>
        </select>
      </FormField>

      <FormField label="Actual Underlying Move (%)" required hint="Enter as a decimal, e.g. -1.5 for −1.5%">
        <input type="number" step="0.1" bind:value={actualMove} placeholder="e.g. -1.5" class="{inputCls}" />
      </FormField>

      <FormField label="Lesson Tags" hint="Comma-separated, optional. e.g. held-too-long, iv-crush">
        <input type="text" bind:value={lessonTagsStr} placeholder="held-too-long, iv-crush" class={inputCls} />
      </FormField>

      {#if error}
        <p class="text-xs text-ctp-red font-semibold">{error}</p>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button variant="danger" disabled={!isValid || isSubmitting} loading={isSubmitting} onclick={handleSubmit}>
      {isSubmitting ? 'Closing…' : 'Confirm Close →'}
    </Button>
  {/snippet}
</Modal>
