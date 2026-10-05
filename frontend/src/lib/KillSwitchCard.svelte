<script lang="ts">
  import { onMount } from 'svelte';
  import { getTradingControl, updateTradingControl, type TradingControlView } from './api';
  import { startPolling } from './poll';
  import { formatLocalDateTime } from './formatters';
  import { toast } from './ui/snackbar.svelte.ts';

  // #1133: Settings' kill switch. It does exactly what the status strip's
  // Halt does — GLOBAL → HALT_ENTRIES with a typed reason — so moving toward
  // safety is one form away from Settings too. It never resumes: the
  // attention block on Home owns every RESUME (#914 owner ruling, ADR-0008),
  // so a halted state here points there instead of offering a button.
  let { onGoHome }: { onGoHome?: () => void } = $props();

  let control    = $state<TradingControlView | null>(null);
  let loadFailed = $state(false);
  let formOpen   = $state(false);
  let reason     = $state('');
  let submitting = $state(false);

  onMount(() => {
    void load();
    startPolling(() => load());
  });

  async function load() {
    try {
      control = await getTradingControl();
      loadFailed = false;
    } catch {
      if (control === null) loadFailed = true;
    }
  }

  const globalControl = $derived(control?.controls.find(c => c.scope === 'GLOBAL') ?? null);
  // Fail closed: no GLOBAL row reads as halted, never as a safe ACTIVE.
  const halted = $derived((control?.sentinel_halt ?? false) || (globalControl?.state ?? 'HALT_ENTRIES') !== 'ACTIVE');

  async function submit(e: Event) {
    e.preventDefault();
    if (reason.trim().length === 0 || submitting) return;
    submitting = true;
    try {
      control = await updateTradingControl('GLOBAL', 'HALT_ENTRIES', reason.trim());
      formOpen = false;
      reason = '';
      toast('GLOBAL entries halted', 'success');
    } catch (err: unknown) {
      toast('Halt failed: ' + (err instanceof Error ? err.message : String(err)), 'error');
    } finally {
      submitting = false;
    }
  }
</script>

<section class="carbon-card p-4 space-y-3 border-ctp-red/40 bg-ctp-red/5" data-testid="kill-switch">
  <h2 class="text-base font-bold text-ctp-text">Kill switch</h2>
  <p class="text-xs text-ctp-subtext0">
    Halts new entries in every book. Exits keep running. Resuming happens only on Home, with a typed reason.
  </p>

  {#if loadFailed}
    <p class="text-xs font-bold text-ctp-red">Control state failed to load. The Halt in the status strip above still works.</p>
  {:else if !control}
    <p class="text-xs text-ctp-overlay0 animate-pulse">Loading control state…</p>
  {:else if halted}
    <div class="text-xs space-y-1" data-testid="kill-switch-halted">
      <p class="font-bold text-ctp-red">
        {control.sentinel_halt ? 'SENTINEL HALT file present' : `GLOBAL ${globalControl?.state ?? 'state unknown'}`}
      </p>
      {#if globalControl && globalControl.state !== 'ACTIVE'}
        <p class="text-ctp-subtext0">{globalControl.reason} ({globalControl.actor}, {formatLocalDateTime(globalControl.changed_at)})</p>
      {/if}
      <button type="button" class="font-bold text-ctp-mauve hover:underline" onclick={onGoHome}
              data-testid="kill-switch-go-home">Review and resume on Home →</button>
    </div>
  {:else if formOpen}
    <form onsubmit={submit} class="flex flex-wrap items-center gap-2" data-testid="kill-switch-form">
      <!-- svelte-ignore a11y_autofocus -->
      <input bind:value={reason} autofocus placeholder="Reason for GLOBAL halt"
             class="min-w-0 flex-1 px-2 py-2 text-sm rounded border border-ctp-surface1 bg-ctp-crust text-ctp-text"
             data-testid="kill-switch-reason" />
      <button type="submit" disabled={reason.trim().length === 0 || submitting}
              class="min-h-11 px-4 rounded-lg bg-ctp-red text-ctp-crust text-sm font-bold disabled:opacity-40"
              data-testid="kill-switch-confirm">Halt entries</button>
      <button type="button" class="text-xs text-ctp-overlay0 hover:underline"
              onclick={() => { formOpen = false; reason = ''; }}>Cancel</button>
    </form>
  {:else}
    <button type="button" onclick={() => { formOpen = true; }}
            class="min-h-11 px-4 rounded-lg bg-ctp-red text-ctp-crust text-sm font-bold"
            data-testid="kill-switch-action">Halt everything</button>
  {/if}
</section>
