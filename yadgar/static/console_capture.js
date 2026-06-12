/**
 * console_capture.js — v5.52.0
 *
 * Proxies window.console (log/info/warn/error/debug) into an in-memory ring
 * buffer capped at ~1 MB. Each entry stores level, message (HTML-escaped),
 * timestamp, and (for error) a stack trace (≤10 frames).
 *
 * API (exported from module):
 *   getEntries(filter?)  — returns shallow copy of buffer; optional level filter
 *   subscribe(fn)        — fn(entry) called on each new entry (after buffer)
 *   unsubscribe(fn)      — remove subscriber
 *   clearBuffer()        — empty the buffer
 *
 * XSS safety: all message strings stored in the buffer as HTML-escaped text.
 * Consumers render via textContent (or innerHTML of the already-escaped string).
 */

const CONSOLE_CAPTURE_MAX_BYTES = 1_048_576; // 1 MB

let _buffer = [];
let _bufferBytes = 0;
const _subscribers = new Set();

/** HTML-escape a value for safe display. */
function _escape(val) {
  const str = typeof val === 'string' ? val : String(val);
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Serialize arguments to a single escaped message string. */
function _formatArgs(args) {
  return Array.from(args).map(_escape).join(' ');
}

/** Rough byte estimate of a buffer entry. */
function _entryBytes(entry) {
  return (entry.message || '').length + (entry.stack ? entry.stack.join('').length : 0) + 64;
}

/** Append entry, evict oldest if over byte cap. */
function _appendEntry(entry) {
  const size = _entryBytes(entry);
  _buffer.push(entry);
  _bufferBytes += size;
  // Evict oldest entries until under cap
  while (_bufferBytes > CONSOLE_CAPTURE_MAX_BYTES && _buffer.length > 0) {
    const evicted = _buffer.shift();
    _bufferBytes -= _entryBytes(evicted);
  }
  for (const fn of _subscribers) {
    try { fn(entry); } catch (_) { /* subscriber errors must not break capture */ }
  }
}

/** Parse stack trace into ≤10 frames. Returns array of strings or null. */
function _parseStack(stack, maxFrames = 10) {
  if (!stack || typeof stack !== 'string') return null;
  const lines = stack
    .split('\n')
    .map(l => l.trim())
    .filter(l => l.length > 0 && l !== 'Error');
  return lines.slice(0, maxFrames);
}

// Save native console methods before proxying
const _native = {};
const _levels = ['log', 'info', 'warn', 'error', 'debug'];
for (const level of _levels) {
  _native[level] = console[level].bind(console);
}

let _paused = false;

/** Install the window.console proxy. Called at DOMContentLoaded. */
function _install() {
  for (const level of _levels) {
    console[level] = function (...args) {
      // Always forward to native console (once, not double-logged)
      _native[level](...args);
      if (_paused) return;

      const entry = {
        level,
        message: _formatArgs(args),
        ts: Date.now(),
      };

      if (level === 'error') {
        // Capture call stack for errors
        try {
          const err = new Error();
          entry.stack = _parseStack(err.stack);
        } catch (_) {
          entry.stack = null;
        }
      }

      _appendEntry(entry);
    };
  }
}

// Public API

/** Return entries, optionally filtered by level. */
export function getEntries(filterLevel) {
  if (filterLevel) {
    return _buffer.filter(e => e.level === filterLevel);
  }
  return [..._buffer];
}

/** Subscribe to new entries. fn(entry) is called for each new entry. */
export function subscribe(fn) {
  _subscribers.add(fn);
}

/** Remove a subscriber. */
export function unsubscribe(fn) {
  _subscribers.delete(fn);
}

/** Clear the buffer. */
export function clearBuffer() {
  _buffer = [];
  _bufferBytes = 0;
}

/** Pause capture (new log calls forwarded to native but not buffered). */
export function pause() {
  _paused = true;
}

/** Resume capture. */
export function resume() {
  _paused = false;
}

/** Return whether capture is paused. */
export function isPaused() {
  return _paused;
}

/** Return buffer byte estimate. */
export function getBufferBytes() {
  return _bufferBytes;
}

// Install proxy at DOMContentLoaded (or immediately if already loaded)
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _install);
  } else {
    _install();
  }
}
