import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['../yadgar/static/**/*.test.js'],
    exclude: [],
  },
});
