import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: false,
    environment: "node",
    include: ["tests/unit/**/*.test.ts"],
    exclude: ["tests/integration/**"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
    },
  },
  resolve: {
    conditions: ["import", "default"],
  },
});
