import { defineConfig } from 'vite';
import { readFileSync, writeFileSync } from 'node:fs';

export default defineConfig({
  build: { outDir: '../cctl/static', emptyOutDir: true, sourcemap: false },
  plugins: [{
    name: 'third-party-notices',
    closeBundle() {
      const notices = ['@xterm/xterm', '@xterm/addon-fit'].map(name =>
        `${name}\n${readFileSync(new URL(`./node_modules/${name}/LICENSE`, import.meta.url), 'utf8')}`
      ).join('\n\n');
      writeFileSync(new URL('../cctl/static/THIRD-PARTY-NOTICES.txt', import.meta.url), notices);
    }
  }]
});
