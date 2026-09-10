import { mount } from 'svelte';
import App from './App.svelte';
import './index.css';
import { registerServiceWorker } from './lib/registerServiceWorker';

registerServiceWorker();

const app = mount(App, {
  target: document.getElementById('app')!,
});

export default app;
