import { describe, it, expect } from "vitest";
import { bearerHeader } from "../../src/auth.js";

describe("bearerHeader", () => {
  it("returns undefined for null token", () => {
    expect(bearerHeader(null)).toBeUndefined();
  });

  it("returns undefined for undefined token", () => {
    expect(bearerHeader(undefined)).toBeUndefined();
  });

  it("returns undefined for empty string", () => {
    expect(bearerHeader("")).toBeUndefined();
  });

  it("returns undefined for whitespace-only string", () => {
    expect(bearerHeader("   ")).toBeUndefined();
  });

  it("returns Bearer header for valid token", () => {
    expect(bearerHeader("my-token")).toBe("Bearer my-token");
  });

  it("trims whitespace from token", () => {
    expect(bearerHeader("  my-token  ")).toBe("Bearer my-token");
  });
});
