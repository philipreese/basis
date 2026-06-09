<script lang="ts">
  type Variant = 'default' | 'warning' | 'danger' | 'success';

  let {
    label,
    value,
    subtext,
    variant = 'default',
  }: {
    label:    string;
    value:    string | number;
    subtext?: string;
    variant?: Variant;
  } = $props();

  const borderClasses: Record<Variant, string> = {
    default: 'border-slate-200 dark:border-slate-800',
    warning: 'border-amber-400 dark:border-amber-500',
    danger:  'border-rose-400 dark:border-rose-500',
    success: 'border-emerald-400 dark:border-emerald-500',
  };

  const valueClasses: Record<Variant, string> = {
    default: 'text-slate-900 dark:text-slate-100',
    warning: 'text-amber-600 dark:text-amber-400',
    danger:  'text-rose-600 dark:text-rose-400',
    success: 'text-emerald-600 dark:text-emerald-400',
  };
</script>

<div class="p-4 rounded-xl border bg-white dark:bg-slate-900 {borderClasses[variant]}">
  <span class="block text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500 mb-1">
    {label}
  </span>
  <span class="block text-xl font-bold carbon-mono {valueClasses[variant]}">
    {value}
  </span>
  {#if subtext}
    <span class="block text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">{subtext}</span>
  {/if}
</div>
