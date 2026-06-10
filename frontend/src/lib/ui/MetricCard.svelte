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
    default: 'border-ctp-surface0',
    warning: 'border-ctp-yellow',
    danger:  'border-ctp-red',
    success: 'border-ctp-green',
  };

  const valueClasses: Record<Variant, string> = {
    default: 'text-ctp-text',
    warning: 'text-ctp-yellow',
    danger:  'text-ctp-red',
    success: 'text-ctp-green',
  };
</script>

<div class="p-4 rounded-lg border bg-ctp-mantle {borderClasses[variant]}">
  <span class="block text-xs font-semibold uppercase tracking-wider text-ctp-overlay0 mb-1">
    {label}
  </span>
  <span class="block text-xl font-bold carbon-mono {valueClasses[variant]}">
    {value}
  </span>
  {#if subtext}
    <span class="block text-xs text-ctp-overlay0 mt-0.5">{subtext}</span>
  {/if}
</div>
