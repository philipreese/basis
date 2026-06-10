export type ToastLevel = 'success' | 'error' | 'info';

export type Toast = {
  id: number;
  message: string;
  level: ToastLevel;
};

let _id = 0;
export let toasts = $state<Toast[]>([]);

export function toast(message: string, level: ToastLevel = 'info', duration = 4000): void {
  const id = ++_id;
  toasts.push({ id, message, level });
  setTimeout(() => {
    const idx = toasts.findIndex(t => t.id === id);
    if (idx !== -1) toasts.splice(idx, 1);
  }, duration);
}

export function dismiss(id: number): void {
  const idx = toasts.findIndex(t => t.id === id);
  if (idx !== -1) toasts.splice(idx, 1);
}
