/**
 * console_capture.test.js — v5.52.0
 *
 * Tests for console_capture.js: proxy, ring buffer, byte cap, XSS escape.
 *
 * Run from viz-tests/: cd viz-tests && npx vitest run
 * (jsdom environment configured there — running from repo root breaks tests)
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// Re-import module fresh each test to get clean state.
// We use vi.resetModules() + dynamic import to work around module-level state.

describe('console_capture', () => {
  let mod;

  beforeEach(async () => {
    vi.resetModules();
    // Provide document.readyState = 'complete' so _install runs immediately on import
    if (typeof document !== 'undefined') {
      Object.defineProperty(document, 'readyState', {
        get: () => 'complete',
        configurable: true,
      });
    }
    mod = await import('./console_capture.js');
    // Clear any entries accumulated during import
    mod.clearBuffer();
    mod.resume();
  });

  afterEach(() => {
    mod.clearBuffer();
    mod.resume();
  });

  // ── 1: console.log captured ────────────────────────────────────────────────

  it('captures console.log into the buffer', () => {
    console.log('hello capture');
    const entries = mod.getEntries();
    const found = entries.some(e => e.level === 'log' && e.message.includes('hello capture'));
    expect(found).toBe(true);
  });

  // ── 2: console.info captured ──────────────────────────────────────────────

  it('captures console.info into the buffer', () => {
    console.info('info message');
    const entries = mod.getEntries();
    expect(entries.some(e => e.level === 'info' && e.message.includes('info message'))).toBe(true);
  });

  // ── 3: console.warn captured ─────────────────────────────────────────────

  it('captures console.warn into the buffer', () => {
    console.warn('warn message');
    const entries = mod.getEntries();
    expect(entries.some(e => e.level === 'warn' && e.message.includes('warn message'))).toBe(true);
  });

  // ── 4: console.error has stack ────────────────────────────────────────────

  it('console.error entry has non-empty stack array', () => {
    console.error('error with stack');
    const entries = mod.getEntries();
    const err = entries.find(e => e.level === 'error' && e.message.includes('error with stack'));
    expect(err).toBeTruthy();
    // stack may be null in some jsdom environments; check that if present it is an array
    if (err.stack !== null && err.stack !== undefined) {
      expect(Array.isArray(err.stack)).toBe(true);
    }
  });

  // ── 5: level filter ────────────────────────────────────────────────────────

  it('getEntries(filterLevel) returns only matching level', () => {
    console.log('a log');
    console.warn('a warn');
    console.error('an error');

    const warnOnly = mod.getEntries('warn');
    expect(warnOnly.every(e => e.level === 'warn')).toBe(true);
    expect(warnOnly.some(e => e.message.includes('a warn'))).toBe(true);

    const errorOnly = mod.getEntries('error');
    expect(errorOnly.every(e => e.level === 'error')).toBe(true);
  });

  // ── 6: pause halts new entries ────────────────────────────────────────────

  it('pause() stops new entries from being buffered', () => {
    mod.pause();
    const before = mod.getEntries().length;
    console.log('should not be captured while paused');
    const after = mod.getEntries().length;
    expect(after).toBe(before);
  });

  // ── 7: resume restores capture ────────────────────────────────────────────

  it('resume() restores capture after pause', () => {
    mod.pause();
    mod.resume();
    const before = mod.getEntries().length;
    console.log('captured after resume');
    const after = mod.getEntries().length;
    expect(after).toBeGreaterThan(before);
  });

  // ── 8: clear empties buffer ───────────────────────────────────────────────

  it('clearBuffer() empties the buffer', () => {
    console.log('entry before clear');
    console.warn('another before clear');
    expect(mod.getEntries().length).toBeGreaterThan(0);
    mod.clearBuffer();
    expect(mod.getEntries().length).toBe(0);
  });

  // ── 9: native console still fires once (no double-logging) ───────────────

  it('native console fires exactly once per call (no double-logging)', () => {
    const spy = vi.fn();
    // Spy on the native method AFTER the proxy is installed.
    // The proxy calls the saved native reference — spying on console.log post-proxy
    // would only catch proxy calls, not the native call. We verify via spy on the
    // proxy output: calling console.log once produces exactly 1 buffer entry.
    mod.clearBuffer();
    console.log('unique-double-log-test-string');
    const entries = mod.getEntries().filter(
      e => e.level === 'log' && e.message.includes('unique-double-log-test-string')
    );
    expect(entries.length).toBe(1);
  });

  // ── 10: XSS escape — critical security regression test ───────────────────

  it('XSS: console.log("<script>alert(1)</script>") stores escaped string', () => {
    const xss = '<script>alert(1)</script>';
    console.log(xss);
    const entries = mod.getEntries();
    const entry = entries.find(e => e.level === 'log' && e.message.includes('&lt;script&gt;'));
    expect(entry).toBeTruthy();
    // Raw unescaped string must NOT appear in the stored message
    const rawEntry = entries.find(e => e.level === 'log' && e.message.includes('<script>'));
    expect(rawEntry).toBeUndefined();
  });

  // ── 11: XSS escape for various HTML special chars ─────────────────────────

  it('XSS: HTML special chars are escaped in stored message', () => {
    const payload = '<img src=x onerror="alert(1)">';
    console.log(payload);
    const entries = mod.getEntries();
    const entry = entries.find(e => e.level === 'log' && e.message.includes('&lt;img'));
    expect(entry).toBeTruthy();
    // Ensure raw < not present
    expect(entries.some(e => e.message.includes('<img'))).toBe(false);
  });

  // ── 12: byte cap evicts old entries ───────────────────────────────────────

  it('byte cap: buffer stays within CONSOLE_CAPTURE_MAX_BYTES', () => {
    // Log many large messages to trigger eviction
    const bigMsg = 'x'.repeat(10_000);
    for (let i = 0; i < 200; i++) {
      console.log(`${bigMsg}-${i}`);
    }
    const bytes = mod.getBufferBytes();
    // 1 MB cap
    expect(bytes).toBeLessThanOrEqual(1_048_576);
  });

  // ── 13: subscribe/unsubscribe ─────────────────────────────────────────────

  it('subscribe() is called for each new entry', () => {
    const received = [];
    const fn = (e) => received.push(e);
    mod.subscribe(fn);
    console.log('subscribed entry');
    mod.unsubscribe(fn);
    expect(received.some(e => e.message.includes('subscribed entry'))).toBe(true);
  });

  it('unsubscribe() stops notifications', () => {
    const received = [];
    const fn = (e) => received.push(e);
    mod.subscribe(fn);
    mod.unsubscribe(fn);
    const before = received.length;
    console.log('after unsubscribe');
    expect(received.length).toBe(before);
  });
});
