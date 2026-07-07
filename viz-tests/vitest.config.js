import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

const repoRoot = resolve(new URL('..', import.meta.url).pathname);

export default defineConfig({
  server: {
    fs: {
      allow: [repoRoot],
    },
  },
  test: {
    environment: 'jsdom',
    include: ['../yadgar/core/static/**/*.test.js'],
    exclude: [],
  },
});
