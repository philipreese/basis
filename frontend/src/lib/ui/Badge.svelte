<script lang="ts">
  type Variant = 'danger' | 'warning' | 'success' | 'info' | 'neutral' | 'indigo' | 'violet';

  let { label, variant = 'neutral' }: { label: string; variant?: Variant } = $props();

  const classes: Record<Variant, string> = {
    danger:  'bg-rose-100 text-rose-700 dark:bg-rose-950/60 dark:text-rose-400',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-400',
    success: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-400',
    info:    'bg-sky-100 text-sky-700 dark:bg-sky-950/60 dark:text-sky-400',
    neutral: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
    indigo:  'bg-indigo-100 text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-400',
    violet:  'bg-violet-100 text-violet-700 dark:bg-violet-950/60 dark:text-violet-400',
  };
</script>

<span class="inline-flex items-center px-2 py-0.5 text-[10px] font-bold tracking-wider uppercase rounded {classes[variant]}">
  {label}
</span>
