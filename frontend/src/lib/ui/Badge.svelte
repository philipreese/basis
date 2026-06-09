<script lang="ts">
  type Variant = 'danger' | 'warning' | 'success' | 'info' | 'neutral' | 'indigo' | 'violet';

  let { label, variant = 'neutral' }: { label: string; variant?: Variant } = $props();

  const classes: Record<Variant, string> = {
    danger:  'bg-ctp-red/15 text-ctp-red border border-ctp-red/30',
    warning: 'bg-ctp-yellow/15 text-ctp-yellow border border-ctp-yellow/30',
    success: 'bg-ctp-green/15 text-ctp-green border border-ctp-green/30',
    info:    'bg-ctp-blue/15 text-ctp-blue border border-ctp-blue/30',
    neutral: 'bg-ctp-surface0 text-ctp-subtext0 border border-ctp-surface1',
    indigo:  'bg-ctp-lavender/15 text-ctp-lavender border border-ctp-lavender/30',
    violet:  'bg-ctp-mauve/15 text-ctp-mauve border border-ctp-mauve/30',
  };
</script>

<span class="inline-flex items-center px-2 py-0.5 text-[10px] font-bold tracking-wider uppercase rounded {classes[variant]}">
  {label}
</span>
