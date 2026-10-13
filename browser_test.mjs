// Browser check for the paragraph skip on a finished story.
// Run from the project folder: node browser_test.mjs
// Requires Node and Playwright. If Playwright is not installed next to this
// project, point PLAYWRIGHT_MODULE at it, for example:
//   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright/index.mjs node browser_test.mjs
// The check is offline: it seeds one finished story, starts the server on a
// free port, drives a real browser, and stops the server. It refuses to run
// while provider keys are set, so no paid service can be called.

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

const ROOT = process.cwd();
const STORY_ID = '20261014-000001-browser-seed';
const PARAGRAPH_STARTS = [0, 4, 8];

let chromium;
try {
  ({ chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright'));
} catch {
  console.error('FAIL: Playwright could not be loaded. Install it (npm install playwright) or set PLAYWRIGHT_MODULE to the playwright module path.');
  process.exit(1);
}

function fail(message) {
  console.error(`FAIL: ${message}`);
  process.exitCode = 1;
}

for (const name of ['ANTHROPIC_API_KEY', 'MISTRAL_API_KEY']) {
  if (process.env[name]) {
    console.error(`FAIL: ${name} is set. The browser check is offline-only; unset the provider keys first.`);
    process.exit(1);
  }
}
const envFile = path.join(ROOT, '.env');
if (fs.existsSync(envFile)) {
  const body = fs.readFileSync(envFile, 'utf8');
  for (const name of ['ANTHROPIC_API_KEY', 'MISTRAL_API_KEY']) {
    const match = body.match(new RegExp(`^${name}=(.*)$`, 'm'));
    if (match && match[1].replace(/['"]/g, '').trim()) {
      console.error(`FAIL: ${name} is set in .env. The browser check is offline-only; unset the provider keys first.`);
      process.exit(1);
    }
  }
}

function wav(seconds) {
  const rate = 24000, data = Buffer.alloc(seconds * rate * 2);
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(36 + data.length, 4); header.write('WAVE', 8);
  header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
  header.write('data', 36); header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

const folder = path.join(ROOT, 'stories', STORY_ID);
function seed() {
  fs.mkdirSync(folder, { recursive: true });
  const story = {
    id: STORY_ID,
    created: '2026-10-14T00:00:01+00:00',
    title: 'The Quiet Rocket',
    choices: { who: 'Robin', hero: 'Robin', where: 'Small village', world: 'Space',
               friend: 'Puppy', mood: 'Calm and sleepy', length: 'Short', voice: 'bf_emma' },
    paragraphs: [
      'Robin and the puppy climbed into the quiet rocket.',
      'The stars waved goodnight as they drifted past.',
      'The little rocket landed softly and everyone slept.',
    ],
    number: 3,
    root_id: STORY_ID,
    next_story_id: null,
    engines: { '1': 'Local fallback', '2': 'Local fallback', '3': 'Local fallback' },
    paragraph_starts: PARAGRAPH_STARTS,
    audio_error: null,
  };
  fs.writeFileSync(path.join(folder, 'story.json'), JSON.stringify(story, null, 2));
  fs.writeFileSync(path.join(folder, 'story.wav'), wav(12));
  fs.writeFileSync(path.join(folder, 'p01.wav'), wav(4));
}

function cleanup() {
  fs.rmSync(folder, { recursive: true, force: true });
}

const freePort = () => new Promise(resolve => {
  const probe = net.createServer();
  probe.listen(0, '127.0.0.1', () => {
    const port = probe.address().port;
    probe.close(() => resolve(port));
  });
});

seed();
const port = await freePort();
const python = fs.existsSync(path.join(ROOT, '.venv', 'bin', 'python')) ? path.join(ROOT, '.venv', 'bin', 'python') : 'python3';
const server = spawn(python, ['-c',
  `import uvicorn, server\nserver.CONFIG.port = ${port}\nuvicorn.run(server.app, host='127.0.0.1', port=${port}, log_level='error')`],
  { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
let serverLog = '';
server.stdout.on('data', chunk => { serverLog += chunk; });
server.stderr.on('data', chunk => { serverLog += chunk; });

let browser;
try {
  const base = `http://127.0.0.1:${port}`;
  let up = false;
  for (let attempt = 0; attempt < 60 && !up; attempt++) {
    try {
      const response = await fetch(`${base}/manifest.webmanifest`);
      up = response.ok;
    } catch { /* not up yet */ }
    if (!up) await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!up) throw new Error(`server did not start on port ${port}\n${serverLog}`);

  browser = await chromium.launch();
  const pageErrors = [];
  const run = async (width, height) => {
    const page = await browser.newPage({ viewport: { width, height } });
    page.on('pageerror', error => pageErrors.push(`${width}px: ${error.message}`));
    await page.goto(base, { waitUntil: 'load', timeout: 15000 });
    await page.waitForFunction(() => !document.querySelector('#make').disabled, null, { timeout: 10000 });
    return page;
  };

  const waitSeek = async (target, label) => {
    try {
      await page.waitForFunction(want => Math.abs(document.querySelector('#audio').currentTime - want) <= 1, target, { timeout: 5000 });
    } catch {
      const actual = await page.evaluate(() => document.querySelector('#audio').currentTime);
      throw new Error(`${label}: currentTime is ${actual.toFixed(2)}s, expected paragraph_starts within 1s of ${target}s (skip is broken: the story restarted)`);
    }
  };

  // The seek behaviour at desktop width.
  let page = await run(1024, 768);
  await page.click('#to-shelf');
  await page.waitForSelector('.book', { timeout: 10000 });
  await page.click('.book');
  await page.waitForFunction(() => document.querySelector('#audio').currentSrc.endsWith('/story.wav'), null, { timeout: 15000 });
  const sourceBefore = await page.evaluate(() => document.querySelector('#audio').currentSrc);

  await page.click('#next-paragraph');
  await waitSeek(4, 'next-paragraph');
  let unchanged = await page.evaluate(() => document.querySelector('#audio').currentSrc);
  if (unchanged !== sourceBefore) throw new Error('audio source changed after next-paragraph');
  const afterNext = await page.evaluate(() => document.querySelector('#audio').currentTime);
  console.log(`PASS next-paragraph: currentTime ${afterNext.toFixed(2)}s is paragraph_starts[1]=${PARAGRAPH_STARTS[1]}s within 1s, audio source unchanged`);

  await page.click('#prev-paragraph');
  await waitSeek(0, 'prev-paragraph');
  unchanged = await page.evaluate(() => document.querySelector('#audio').currentSrc);
  if (unchanged !== sourceBefore) throw new Error('audio source changed after prev-paragraph');
  const afterPrev = await page.evaluate(() => document.querySelector('#audio').currentTime);
  console.log(`PASS prev-paragraph: currentTime ${afterPrev.toFixed(2)}s is paragraph_starts[0]=${PARAGRAPH_STARTS[0]}s within 1s, audio source unchanged`);
  await page.close();

  // The page runs clean at phone width: both place maps feed the UI.
  page = await run(390, 844);
  for (let guard = 0; guard < 10; guard++) {
    const step = await page.evaluate(() => document.querySelector('.step.active').dataset.step);
    if (step === 'ready') break;
    if (step === 'where') await page.check('input[name="where"][value="Paris"]');
    await page.click('.step.active .next', { timeout: 5000 });
    await page.waitForFunction(previous => document.querySelector('.step.active').dataset.step !== previous, step, { timeout: 5000 });
  }
  const summary = await page.textContent('#summary');
  if (!summary.includes('in Paris at')) throw new Error(`place phrases broken: summary is "${summary.trim()}"`);
  const chipPic = await page.evaluate(() => {
    const chip = document.querySelectorAll('#chips .chip')[1];
    const svg = chip && chip.querySelector('svg');
    return svg ? svg.outerHTML.length : 0;
  });
  if (chipPic < 200) throw new Error(`place pictures broken: where chip SVG is ${chipPic} chars`);
  console.log('PASS 390px: place phrases in the summary, place pictures in the chips, no page errors');
  await page.close();

  if (pageErrors.length) throw new Error(`page errors:\n${pageErrors.join('\n')}`);
  console.log('PASS zero page errors at 1024 and 390 px');
  console.log('PASS browser check');
} catch (error) {
  fail(error.message);
  if (String(error.message).includes('did not start')) console.error(serverLog);
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  cleanup();
}
process.exit(process.exitCode || 0);
