<script lang="ts">
  import {
    updateTradingControl, resolveReconciliation, resolvePartialOrder, ackFlexDiscrepancies,
    type AttentionRowItem,
  } from './api';
  import { toast } from './ui/snackbar.svelte.ts';
  import { IconWarning, IconInfo } from './ui/icons';

  let {
    item,
    informational = false,
    onClosePosition,
    onNavigate,
    onResolved,
  }: {
    item: AttentionRowItem;
    informational?: boolean;
    onClosePosition?: (positionId: string) => void;
    onNavigate?: (tab: string, anchor?: string) => void;
    onResolved?: () => void;
  } = $props();

  let formOpen    = $state(false);
  let reason      = $state('');
  let submitting  = $state(false);

  // One component drives every mutating action kind (DESIGN-890.md §3/§4) —
  // each kind's endpoint takes a different body shape, so the target fields
  // the backend attached to the action are unpacked per kind here rather
  // than forwarded blindly. Which kinds NEED the reason step at all is a
  // separate question, answered by action.requires_reason (#915) — not by
  // this switch.
  async function performAction(value: string) {
    submitting = true;
    const target = item.action.target ?? {};
    try {
      switch (item.action.kind) {
        case 'ack_halt':
          await updateTradingControl(target.scope as string, 'ACTIVE', value);
          break;
        case 'resolve_reconciliation':
          await resolveReconciliation(Number(target.run_id), value);
          break;
        case 'resolve_partial_order':
          await resolvePartialOrder(target.order_ref as string, value);
          break;
        case 'flex_ack':
          await ackFlexDiscrepancies(target.exec_ids as string[], value);
          break;
        default:
          return;
      }
      toast(`${item.title} — done`, 'success', 3000);
      formOpen = false;
      onResolved?.();
    } catch (err: unknown) {
      toast('Failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      submitting = false;
    }
  }

  async function submit(e: Event) {
    e.preventDefault();
    if (reason.trim().length === 0 || submitting) return;
    await performAction(reason.trim());
  }

  function handleAction() {
    if (item.action.kind === 'close_position') {
      const positionId = item.action.target?.position_id;
      if (typeof positionId === 'string') onClosePosition?.(positionId);
      return;
    }
    if (item.action.kind === 'view_only') {
      onNavigate?.(item.action.navigate_to ?? '');
      return;
    }
    if (item.action.requires_reason) {
      reason = '';
      formOpen = true;
      return;
    }
    if (submitting) return;
    void performAction('');
  }
</script>

<div class="p-3.5 {informational ? '' : 'sm:flex sm:items-start sm:justify-between sm:gap-3'}"
     data-testid="attention-item-{item.id}">
  <div class="flex items-start gap-2 min-w-0">
    <span class="mt-0.5 shrink-0 {informational ? 'text-ctp-overlay0' : 'text-ctp-red'}">
      {#if informational}<IconInfo size={13} strokeWidth={2} />{:else}<IconWarning size={13} strokeWidth={2} />{/if}
    </span>
    <div class="min-w-0">
      <p class="text-xs font-bold text-ctp-text">{item.title}</p>
      {#if item.detail}
        <p class="text-xs text-ctp-subtext0 mt-0.5 break-words">{item.detail}</p>
      {/if}
      {#if item.meta}
        <p class="text-[11px] text-ctp-overlay0 mt-0.5">{item.meta}</p>
      {/if}
    </div>
  </div>

  {#if informational}
    <!-- VIEW_ONLY keeps its navigate affordance even in the informational
         section (it has somewhere to send you); ACKNOWLEDGE_ONLY has
         nothing to do but be seen — the kind genuinely differs here. -->
    {#if item.action.kind === 'view_only'}
      <button onclick={handleAction} data-testid="attention-item-{item.id}-action"
              class="text-[11px] font-semibold text-ctp-mauve hover:underline ml-5 sm:ml-0">
        {item.action.label}
      </button>
    {:else}
      <span class="text-[11px] font-semibold text-ctp-overlay0 ml-5 sm:ml-0" data-testid="attention-item-{item.id}-seen">
        {item.action.label}
      </span>
    {/if}
  {:else if formOpen}
    <form onsubmit={submit} class="mt-2 sm:mt-0 flex items-center gap-2 shrink-0" data-testid="attention-item-{item.id}-form">
      <!-- svelte-ignore a11y_autofocus -->
      <input type="text" bind:value={reason} autofocus
             placeholder="reason (required)"
             data-testid="attention-item-{item.id}-reason"
             class="flex-1 sm:w-56 px-2 py-1 text-xs border border-ctp-surface1 rounded bg-ctp-crust text-ctp-text focus:outline-none focus:ring-1 focus:ring-ctp-mauve" />
      <button type="submit" disabled={reason.trim().length === 0 || submitting}
              data-testid="attention-item-{item.id}-confirm"
              class="px-2 py-1 text-xs font-bold rounded bg-ctp-red/20 text-ctp-red disabled:opacity-40">
        Confirm
      </button>
      <button type="button" class="text-xs text-ctp-overlay0 hover:underline"
              onclick={() => (formOpen = false)}>
        Cancel
      </button>
    </form>
  {:else}
    <button onclick={handleAction} data-testid="attention-item-{item.id}-action"
            class="mt-2 sm:mt-0 shrink-0 px-3 py-1.5 text-xs font-bold rounded-lg bg-ctp-red/10 text-ctp-red hover:bg-ctp-red/20 transition-colors">
      {item.action.label}
    </button>
  {/if}
</div>
