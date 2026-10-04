'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { probe } = require('../lib/mcp-probe');
const commandcode = require('../lib/harnesses/commandcode');
const fixture = path.join(__dirname, 'fixtures', 'mcp-server.js');
const entry = { command: process.execPath, args: [fixture] };

test('probe performs initialize and tools/list then closes stdin', async () => {
  const result = await probe(entry);
  assert.equal(result.ok, true);
  assert.deepEqual(result.tools, ['browser_read']);
});

test('probe reports startup errors and redacts secrets', async () => {
  const result = await probe(entry, { env: { ...process.env, JEV_TEST_MODE: 'exit', JEV_TEST_SECRET: 'private-test-key' } });
  assert.equal(result.ok, false);
  assert.match(result.error, /startup failed \[redacted\]/);
  assert.ok(!result.error.includes('private-test-key'));
});

test('probe reports missing executable', async () => {
  const result = await probe({ command: path.join(os.tmpdir(), 'jev-definitely-missing'), args: [] });
  assert.equal(result.ok, false);
  assert.match(result.error, /ENOENT/);
});

test('probe rejects contaminated stdout', async () => {
  const result = await probe(entry, { env: { ...process.env, JEV_TEST_MODE: 'garbage' } });
  assert.equal(result.ok, false);
  assert.match(result.error, /Non-JSON/);
});

test('probe times out instead of hanging', async () => {
  const result = await probe(entry, { timeout: 300, env: { ...process.env, JEV_TEST_MODE: 'timeout' } });
  assert.equal(result.ok, false);
  assert.match(result.error, /timed out/);
});

test('Command Code shell launch handles Program Files and Danish Javaid paths', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-shell-'));
  try {
    const binary = path.join(dir, 'Program Files', 'nodejs', process.platform === 'win32' ? 'node.exe' : 'node');
    const script = path.join(dir, 'Users', 'Danish Javaid', 'mcp.js');
    fs.mkdirSync(path.dirname(binary), { recursive: true });
    fs.mkdirSync(path.dirname(script), { recursive: true });
    // Avoid copying the executable where symlinks work without elevation.
    if (process.platform === 'win32') fs.copyFileSync(process.execPath, binary);
    else fs.symlinkSync(process.execPath, binary);
    fs.copyFileSync(fixture, script);
    const broken = await probe({ command: binary, args: [script] }, { shell: true });
    assert.equal(broken.ok, false, 'unquoted shell paths reproduce the original failure');
    const fixed = await probe({ command: commandcode.quoteWindows(binary), args: [commandcode.quoteWindows(script)] }, { shell: true });
    assert.equal(fixed.ok, true, fixed.error);
    assert.deepEqual(fixed.tools, ['browser_read']);
    // Let the successfully discovered server exit on EOF before removing it.
    await new Promise((resolve) => setTimeout(resolve, 100));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('real Node-to-Python launcher discovers browser and mobile tools outside project cwd', async (t) => {
  const runtime = require('../lib/runtime');
  if (!runtime.resolve()) return t.skip('Python runtime not installed');
  const real = { command: process.execPath, args: [path.resolve(__dirname, '..', 'bin', 'jev-use-mcp.js')] };
  const result = await probe(real, { cwd: os.tmpdir() });
  assert.equal(result.ok, true, result.error);
  assert.equal(result.tools.length, 15);
  assert.ok(result.tools.includes('browser_profiles'));
  assert.ok(result.tools.includes('browser_action'));
  assert.ok(result.tools.includes('android_use'));
  assert.ok(result.tools.includes('android_facebook'));
  assert.ok(result.tools.includes('android_facebook_flow'));
  if (process.platform === 'win32') {
    const registered = { command: commandcode.quoteWindows(real.command), args: real.args.map(commandcode.quoteWindows) };
    const shellResult = await probe(registered, { shell: true, cwd: os.tmpdir() });
    assert.equal(shellResult.ok, true, shellResult.error);
    assert.equal(shellResult.tools.length, 15);
  }
});
