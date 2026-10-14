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
const CHAIN_ID = '20261013-235959-browser-first-night';
const QPROBE_ID = '20261012-120000-browser-qprobe';
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
const chainFolder = path.join(ROOT, 'stories', CHAIN_ID);
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
  fs.mkdirSync(chainFolder, { recursive: true });
  const first = { ...story, id: CHAIN_ID, created: '2026-10-13T23:59:59+00:00', title: 'The First Night', number: 1, next_story_id: STORY_ID };
  delete first.paragraph_starts;
  fs.writeFileSync(path.join(chainFolder, 'story.json'), JSON.stringify(first, null, 2));
  fs.writeFileSync(path.join(chainFolder, 'story.wav'), wav(12));
  fs.writeFileSync(path.join(chainFolder, 'p01.wav'), wav(4));
  const qFolder = path.join(ROOT, 'stories', QPROBE_ID);
  fs.mkdirSync(qFolder, { recursive: true });
  const qprobe = { ...story, id: QPROBE_ID, created: '2026-10-12T12:00:00+00:00', title: 'The Wobbly Comet', number: 1 };
  fs.writeFileSync(path.join(qFolder, 'story.json'), JSON.stringify(qprobe, null, 2));
  for (const name of ['story.wav', 'p01.wav', 'p02.wav', 'p03.wav', 'q01.wav', 'q02.wav', 'q03.wav']) fs.writeFileSync(path.join(qFolder, name), wav(4));
}

function cleanup() {
  fs.rmSync(folder, { recursive: true, force: true });
  fs.rmSync(chainFolder, { recursive: true, force: true });
  fs.rmSync(path.join(ROOT, 'stories', QPROBE_ID), { recursive: true, force: true });
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

  // MAY-706 item 1: a classic opens on one tap and plays its pre-rendered MP3.
  {
    const page = await run(1180, 820);
    await page.click('#to-classics');
    await page.waitForSelector('#classics-books .book', { timeout: 10000 });
    await page.click('#classics-books .book');
    await page.waitForFunction(() => document.querySelector('#audio').currentSrc.endsWith('.mp3'), null, { timeout: 15000 });
    let playing = false;
    try { await page.waitForFunction(() => !document.querySelector('#audio').paused, null, { timeout: 5000 }); playing = true; } catch { /* reported below */ }
    if (!playing) throw new Error('item 1: the classic did not start playing its pre-rendered audio');
    console.log('PASS item 1: a classic opens on one tap and plays its pre-rendered MP3');
    await page.close();
  }

  // MAY-706 items 2 and 3: everything fits both iPad sizes, and the bars are safe-area padded.
  for (const [width, height] of [[1180, 820], [820, 1180]]) {
    const page = await run(width, height);
    const fit = await page.evaluate(() => {
      const cut = [...document.querySelectorAll('body *')].filter(el => {
        if (getComputedStyle(el).position === 'fixed') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.right > window.innerWidth + 1;
      });
      return { scroll: document.documentElement.scrollWidth, inner: window.innerWidth, cut: cut.length };
    });
    if (fit.scroll > fit.inner) throw new Error(`item 2 at ${width}x${height}: scrollWidth ${fit.scroll} > innerWidth ${fit.inner}`);
    if (fit.cut) throw new Error(`item 2 at ${width}x${height}: ${fit.cut} element(s) cut off at the right edge`);
    const safeTop = await page.evaluate(() =>
      /\.top\s*\{[^}]*safe-area-inset-top/.test(document.querySelector('style').textContent));;
    if (!safeTop) throw new Error(`item 3 at ${width}x${height}: the header has no safe-area top padding`);
    console.log(`PASS item 2+3 ${width}x${height}: scrollWidth ${fit.scroll} <= innerWidth ${fit.inner}, nothing cut at the edge, header safe-area padded`);
    await page.close();
  }

  // MAY-706 items 4, 5 and 6: one tap opens and plays, back returns to the collection, story back/next work.
  {
    const page = await run(1180, 820);
    await page.click('#to-shelf');
    await page.waitForSelector('.book', { timeout: 10000 });
    await page.waitForTimeout(1000); // let the card rise animation settle
    // Tap near the top edge of the card, where a dead strip used to swallow the first tap.
    const row = await page.locator('.saved-row').first().boundingBox();
    await page.mouse.click(row.x + 60, row.y + 8);
    await page.waitForFunction(() => !document.querySelector('#play').hidden, null, { timeout: 5000 });
    let playing = false;
    try { await page.waitForFunction(() => !document.querySelector('#audio').paused, null, { timeout: 5000 }); playing = true; } catch { /* reported below */ }
    if (!playing) throw new Error('item 5: one tap on a saved story must open and play it');
    const backLabel = (await page.textContent('#back')).trim();
    if (backLabel !== 'Stories') throw new Error(`item 4: back label is "${backLabel}", expected "Stories"`);
    await page.click('#back');
    await page.waitForFunction(() => !document.querySelector('#shelf').hidden && document.querySelector('#play').hidden, null, { timeout: 5000 });
    console.log('PASS item 4: back from a story returns to the Stories collection');
    console.log('PASS item 5: one tap opens and plays a saved story');

    await page.click('.book');
    await page.waitForFunction(() => !document.querySelector('#play').hidden, null, { timeout: 5000 });
    const title = (await page.textContent('#title')).trim();
    await page.click('#prev-story');
    await page.waitForFunction(t => document.querySelector('#title').textContent.trim() !== t, title, { timeout: 5000 });
    if ((await page.textContent('#title')).trim() === title) throw new Error('item 6: story back did not open the previous story');
    await page.click('#next-story');
    await page.waitForFunction(t => document.querySelector('#title').textContent.trim() === t, title, { timeout: 5000 });
    await page.click('#next-story');
    await page.waitForFunction(() => /last story tonight/.test(document.querySelector('#status').textContent), null, { timeout: 3000 });
    console.log('PASS item 6: story back opens the previous story, story next returns and reports the last story');
    await page.close();
  }

  // MAY-706 item 8: a child can type their own place in the Where step.
  {
    const page = await run(1180, 820);
    for (let guard = 0; guard < 12; guard++) {
      const step = await page.evaluate(() => document.querySelector('.step.active').dataset.step);
      if (step === 'ready') break;
      if (step === 'where') {
        await page.click('label:has(input[value="__own"])');
        await page.fill('#own-place', "Grandma's garden");
      }
      await page.click('.step.active .next', { timeout: 5000 });
      await page.waitForFunction(previous => document.querySelector('.step.active').dataset.step !== previous, step, { timeout: 5000 });
    }
    const summary = await page.textContent('#summary');
    if (!summary.includes("Grandma's garden")) throw new Error(`item 8: own place missing from the summary: "${summary.trim()}"`);
    const chip = await page.evaluate(() => document.querySelectorAll('#chips .chip')[1].querySelector('.sr').textContent);
    if (!chip.includes("Grandma's garden")) throw new Error(`item 8: own place missing from the chips: "${chip}"`);
    console.log("PASS item 8: typing our own place reaches the summary and the chips");
    await page.close();
  }

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

  // MAY-725 a: the Stories page stays quiet while idle.
  {
    const context = await browser.newContext({ viewport: { width: 1180, height: 820 }, hasTouch: true });
    const page = await context.newPage();
    await page.goto(base, { waitUntil: 'load', timeout: 15000 });
    await page.waitForFunction(() => !document.querySelector('#make').disabled, null, { timeout: 10000 });
    let storiesRequests = 0;
    page.on('request', req => { if (new URL(req.url()).pathname === '/api/stories') storiesRequests++; });
    await page.click('#to-shelf');
    await page.waitForSelector('#books .saved-row', { timeout: 10000 });
    await page.waitForTimeout(10000);
    if (storiesRequests >= 3) throw new Error(`MAY-725 a: ${storiesRequests} /api/stories requests in 10 s idle, expected fewer than 3`);
    console.log(`PASS MAY-725 a: ${storiesRequests} /api/stories requests in 10 s idle on the Stories page (fewer than 3)`);
    await context.close();
  }

  // MAY-725 b: one tap archives on the iPad (portrait, touch), and Restore moves the story back.
  {
    const context = await browser.newContext({ viewport: { width: 820, height: 1180 }, hasTouch: true });
    const page = await context.newPage();
    await page.goto(base, { waitUntil: 'load', timeout: 15000 });
    await page.waitForFunction(() => !document.querySelector('#make').disabled, null, { timeout: 10000 });
    await page.click('#to-shelf');
    await page.waitForSelector('#books .saved-row', { timeout: 10000 });
    await page.waitForTimeout(1000); // let the card rise animation settle before the touch lands
    const title = (await page.locator('#books .saved-row .book strong').first().textContent()).trim();
    const rows = await page.locator('#books .saved-row').count();
    await page.locator('#books .saved-row .pill.small').first().tap();
    await page.waitForFunction(n => document.querySelectorAll('#books .saved-row').length === n - 1, rows, { timeout: 5000 });
    await page.click('#shelf-back');
    await page.click('#to-archive');
    await page.waitForFunction(t => [...document.querySelectorAll('#archive-books .saved-row .book strong')].some(el => el.textContent.trim() === t), title, { timeout: 5000 });
    await page.locator('#archive-books .saved-row .pill.small').first().tap();
    await page.waitForFunction(t => [...document.querySelectorAll('#archive-books .saved-row .book strong')].every(el => el.textContent.trim() !== t), title, { timeout: 5000 });
    await page.click('#archive-back');
    await page.click('#to-shelf');
    await page.waitForFunction(t => [...document.querySelectorAll('#books .saved-row .book strong')].some(el => el.textContent.trim() === t), title, { timeout: 5000 });
    console.log('PASS MAY-725 b: one tap archives on the iPad, the story leaves the shelf and Restore moves it back');
    await context.close();
  }

  // MAY-725 c: an old story with a vanished audio part plays without any 404.
  {
    const page = await run(1180, 820);
    await page.click('#to-shelf');
    await page.waitForSelector('#books .saved-row', { timeout: 10000 });
    const notFound = [];
    page.on('response', res => { if (res.status() === 404) notFound.push(res.url()); });
    fs.rmSync(path.join(ROOT, 'stories', QPROBE_ID, 'q01.wav'));
    await page.locator('#books .book').nth(2).click();
    await page.waitForFunction(() => document.querySelector('#audio').currentSrc.endsWith('.wav') && !document.querySelector('#audio').paused, null, { timeout: 10000 });
    await page.waitForTimeout(2500);
    if (notFound.length) throw new Error(`MAY-725 c: ${notFound.length} x 404 (${notFound.join(', ')})`);
    console.log('PASS MAY-725 c: zero 404 responses when an old story plays with a missing audio part');
    await page.close();
  }

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
