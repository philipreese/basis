<script lang="ts">
  import type { RollPositionRequest, ScannedPosition } from './api';
  import Modal from './ui/Modal.svelte';
  import FormField from './ui/FormField.svelte';
  import Button from './ui/Button.svelte';
  import { formatDollar } from './formatters';

  let {
    position,
    onConfirm,
    onCancel,
  }: {
    position: ScannedPosition;
    onConfirm: (positionId: string, req: RollPositionRequest) => Promise<void>;
    onCancel: () => void;
  } = $props();

  // Initial-value snapshots are intentional: the modal is remounted per roll.
  // svelte-ignore state_referenced_locally
  const roll = position.roll!;
  const suggested = roll.suggested_legs ?? [];
  // svelte-ignore state_referenced_locally
  const isPut = position.legs[0]?.option_type === 'PUT';

  // Numeric inputs bind to number|null — Svelte coerces type=number bindings.
  // svelte-ignore state_referenced_locally
  let closeCost     = $state<number | null>(position.current_value_per_share);
  let newCredit     = $state<number | null>(null);
  let newExpiration = $state(roll.suggested_expiration ?? '');
  let shortStrike   = $state<number | null>(suggested.find(l => l.direction === 'SHORT')?.strike ?? null);
  let longStrike    = $state<number | null>(suggested.find(l => l.direction === 'LONG')?.strike ?? null);
  let isSubmitting  = $state(false);
  let error         = $state('');

  const netCredit = $derived(
    closeCost !== null && newCredit !== null ? Math.round((newCredit - closeCost) * 100) / 100 : null,
  );
  const isValid = $derived(
    closeCost !== null && newCredit !== null && netCredit !== null && netCredit > 0 &&
    newExpiration !== '' && shortStrike !== null && longStrike !== null
  );

  async function handleSubmit() {
    if (!isValid || closeCost === null || newCredit === null || shortStrike === null || longStrike === null) return;
    isSubmitting = true;
    error = '';
    const optionType = isPut ? ('PUT' as const) : ('CALL' as const);
    try {
      await onConfirm(position.position_id, {
        close_cost_per_share: closeCost,
        new_credit_per_share: newCredit,
        new_expiration: newExpiration,
        new_legs: [
          { option_type: optionType, direction: 'SHORT', strike: shortStrike, expiration: newExpiration },
          { option_type: optionType, direction: 'LONG', strike: longStrike, expiration: newExpiration },
        ],
      });
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : 'Failed to roll position';
    } finally {
      isSubmitting = false;
    }
  }

  const inputCls = 'w-full mt-1 px-3 py-2 border border-ctp-surface1 rounded-lg bg-ctp-crust text-ctp-text text-sm focus:outline-none focus:ring-2 focus:ring-ctp-mauve carbon-mono';
</script>

<Modal title="Roll Position" onclose={onCancel}>
  {#snippet body()}
    <div class="space-y-4">
      <p class="text-xs text-ctp-subtext0 bg-ctp-crust rounded-lg px-3 py-2 border border-ctp-surface0 carbon-mono">
        Position: <span class="font-bold text-ctp-text">{position.position_id}</span>
        · roll {roll.rolls_used + 1} of {roll.rolls_max}
      </p>
      <p class="text-xs text-ctp-subtext0 leading-relaxed">{roll.reason}</p>

      <div class="grid grid-cols-2 gap-3">
        <FormField label="Close Current Spread ($/share)" required hint="Buyback cost of the existing legs">
          <input type="number" step="0.01" bind:value={closeCost} class={inputCls} data-testid="roll-close-cost" />
        </FormField>
        <FormField label="New Spread Credit ($/share)" required hint="Credit received for the new legs">
          <input type="number" step="0.01" bind:value={newCredit} placeholder="e.g. 1.75" class={inputCls} data-testid="roll-new-credit" />
        </FormField>
      </div>

      <div class="grid grid-cols-3 gap-3">
        <FormField label="New Short Strike" required>
          <input type="number" step="1" bind:value={shortStrike} class={inputCls} />
        </FormField>
        <FormField label="New Long Strike" required>
          <input type="number" step="1" bind:value={longStrike} class={inputCls} />
        </FormField>
        <FormField label="New Expiration" required>
          <input type="date" bind:value={newExpiration} class={inputCls} />
        </FormField>
      </div>

      <div class="px-3 py-2 rounded-lg border carbon-mono text-sm font-bold
        {netCredit === null ? 'border-ctp-surface0 text-ctp-overlay0'
          : netCredit > 0 ? 'border-ctp-green/40 bg-ctp-green/10 text-ctp-green'
          : 'border-ctp-red/40 bg-ctp-red/10 text-ctp-red'}"
        data-testid="roll-net">
        {#if netCredit === null}
          Net credit: enter both prices
        {:else if netCredit > 0}
          Net credit {formatDollar(netCredit)}/share
        {:else}
          Net DEBIT {formatDollar(netCredit)}/share — debit rolls are blocked; take the loss instead
        {/if}
      </div>

      <p class="text-[11px] text-ctp-overlay0">
        Rules: net credit only · max {roll.rolls_max} rolls · {isPut ? 'puts roll DOWN and out' : 'calls roll UP and out'}
        — later expiration required.
      </p>

      {#if error}
        <p class="text-xs text-ctp-red font-semibold">{error}</p>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" disabled={!isValid || isSubmitting} loading={isSubmitting} onclick={handleSubmit}>
      {isSubmitting ? 'Rolling…' : 'Confirm Roll ↻'}
    </Button>
  {/snippet}
</Modal>
