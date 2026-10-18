// Play and learn: five calm activities for a child of 7 or more, around the bedtime story.
// Nothing here talks to a model or leaves the home; drawings stay in this browser's storage.
// Loaded after the player script in index.html, which provides window.storytime.
(() => {
const st = window.storytime;
const $ = id => document.getElementById(id);
const body = $('learn-body'), status = $('learn-status');

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, onClick, className = 'pill') {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}
const picture = symbol => `<svg class="pic" viewBox="0 0 120 120" aria-hidden="true"><use href="#${symbol}"/></svg>`;
const FOREST = '<svg class="pic" viewBox="300 60 400 340" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><rect x="300" y="-600" width="400" height="1020" fill="url(#g-dusk)"/><use href="#bg-forest"/></svg>';
function shuffle(list) {
  const copy = [...list];
  for (let i = copy.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}
async function library() {
  const [made, classics] = await Promise.all([st.request('/api/stories'), st.request('/api/classics')]);
  return {made, classics};
}

/* ---------- the menu and the frame every activity shares ---------- */
const ACTIVITIES = [
  {id: 'word', title: 'Word of the night', blurb: 'A new word to listen for', time: '1 minute', art: picture('f-owl'), run: () => wordOfTheNight(0)},
  {id: 'quiz', title: 'Did you hear?', blurb: 'Three questions about a story', time: '2 minutes', art: picture('f-puppy'), run: () => pickQuiz()},
  {id: 'path', title: 'Choose the path', blurb: 'Read it yourself and pick what happens', time: '5 minutes', art: FOREST, run: () => pickPath()},
  {id: 'pairs', title: 'Story pairs', blurb: 'Match each picture to its word', time: '3 minutes', art: picture('f-fox'), run: () => pairs()},
  {id: 'draw', title: 'Draw your hero', blurb: 'Draw the hero of your story', time: '5 minutes', art: picture('f-unicorn'), run: () => draw()}
];
function menu() {
  status.textContent = '';
  $('learn').classList.remove('in-activity');
  const grid = el('div', 'activities');
  ACTIVITIES.forEach((activity, i) => {
    const tile = button('', activity.run, 'activity');
    tile.id = `do-${activity.id}`;
    tile.style.setProperty('--i', i);
    tile.innerHTML = activity.art;
    tile.append(el('strong', '', activity.title), el('span', 'blurb', activity.blurb), el('span', 'time', activity.time));
    grid.append(tile);
  });
  body.replaceChildren(grid);
}
function panel(title) {
  status.textContent = '';
  $('learn').classList.add('in-activity');
  const back = button('', menu, 'pill');
  back.id = 'activity-back';
  back.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-back"/></svg>';
  back.append('All activities');
  const heading = el('h2', 'activity-title', title);
  heading.tabIndex = -1;
  const head = el('div', 'activity-head'), area = el('div', 'activity-area');
  head.append(back, heading);
  body.replaceChildren(head, area);
  heading.focus({preventScroll: true});
  window.scrollTo(0, 0);
  return area;
}
function finish(area, text, again, againLabel = 'Play again') {
  const done = el('div', 'done');
  done.id = 'activity-done';
  done.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-star"/></svg>';
  const message = el('p', '', text);
  message.tabIndex = -1;
  const row = el('div', 'learn-row');
  if (again) row.append(button(againLabel, again, 'make'));
  row.append(button('All activities', menu));
  done.append(message, row);
  area.replaceChildren(done);
  message.focus({preventScroll: true});
}
function storyCard(s, onClick) {
  const card = button('', onClick, 'book');
  card.innerHTML = s.classic ? st.classicCover() : st.cover(s.choices);
  const meta = el('span', 'meta');
  meta.append(el('strong', '', s.title), el('span', 'when', s.classic ? s.source : `${st.readers()[st.readerOf(s)] || 'Someone'} read it`));
  card.append(meta);
  return card;
}

/* ---------- 1. word of the night: learn a word, then hear it glow in the story ---------- */
async function wordOfTheNight(offset) {
  const area = panel('Word of the night');
  let made, classics;
  try { ({made, classics} = await library()); } catch { status.textContent = 'The stories could not be loaded. Please try again.'; return; }
  // Tonight's own story comes first; otherwise a classic, a different one each night.
  const today = new Date().toLocaleDateString('en-CA');
  const tonight = made.filter(s => s.word && s.created.startsWith(today));
  const tales = classics.filter(s => s.word);
  const start = Math.floor(Date.now() / 86400000) % Math.max(1, tales.length);
  const choices = [...tonight, ...tales.slice(start), ...tales.slice(0, start)];
  if (!choices.length) { area.append(el('p', 'hint', 'No word tonight yet. Make a story first.')); return; }
  const s = choices[offset % choices.length], word = s.word.word;
  const pattern = new RegExp('\\b' + word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'i');
  const sentence = (s.paragraphs || []).flatMap(p => p.split(/(?<=[.!?])\s+/)).find(line => pattern.test(line));
  const card = el('div', 'word-card');
  card.append(el('p', 'word-big', word), el('p', 'word-meaning', s.word.meaning));
  if (sentence) {
    const example = el('p', 'word-example'), [before, ...after] = sentence.split(pattern);
    example.append(before, el('mark', '', sentence.match(pattern)[0]), after.join(word));
    card.append(example);
  }
  const from = el('p', 'word-from', 'From ');
  from.append(el('b', '', s.title));
  card.append(from);
  const listen = button('Listen for it', () => st.open(s.id), 'make');
  listen.id = 'word-listen';
  const row = el('div', 'learn-row');
  row.append(listen);
  if (choices.length > 1) row.append(button('Another word', () => wordOfTheNight(offset + 1)));
  area.append(card, row);
}

/* ---------- 2. did you hear? three gentle questions, no score ---------- */
async function pickQuiz() {
  const area = panel('Did you hear?');
  let made, classics;
  try { ({made, classics} = await library()); } catch { status.textContent = 'The stories could not be loaded. Please try again.'; return; }
  const stories = [...made.filter(s => s.quiz).slice(0, 4), ...classics.filter(s => s.quiz)];
  area.append(el('p', 'hint', 'Which story did you listen to?'));
  const list = el('div', 'story-pick');
  list.append(...stories.map(s => storyCard(s, () => quiz(s))));
  area.append(list);
}
function quiz(s) {
  const area = panel('Did you hear?');
  ask(area, s, 0);
}
function ask(area, s, number) {
  const question = s.quiz[number], box = el('div', 'question');
  box.append(el('p', 'quiz-count', `${s.title} · question ${number + 1} of 3`), el('p', 'quiz-q', question.q));
  const answers = el('div', 'answers'), reply = el('p', 'reply');
  reply.setAttribute('role', 'status');
  const choose = (picked, right) => {
    answers.querySelectorAll('button').forEach(b => { b.disabled = true; b.classList.toggle('right', b.dataset.right === 'true'); });
    reply.textContent = right ? `Yes! ${question.answers[0]}.` : `Good try. It was: ${question.answers[0]}.`;
    const next = number < 2
      ? button('Next question', () => ask(area, s, number + 1), 'make')
      : button('Finish', () => finish(area, 'Three questions. You listened well.', () => quiz(s), 'Ask me again'), 'make');
    next.id = 'quiz-next';
    box.append(next);
    next.focus({preventScroll: true});
  };
  shuffle(question.answers.map((text, i) => ({text, right: i === 0}))).forEach(answer => {
    const b = button(answer.text, () => choose(b, answer.right), 'answer');
    b.dataset.right = String(answer.right);
    answers.append(b);
  });
  box.append(answers, reply);
  area.replaceChildren(box);
}

/* ---------- 3. choose the path: a short tale he reads himself ---------- */
let paths = null;
async function pickPath() {
  const area = panel('Choose the path');
  try { paths = paths || await st.request('/api/paths'); } catch { status.textContent = 'The tales could not be loaded. Please try again.'; return; }
  area.append(el('p', 'hint', 'Read the page, then choose what happens next.'));
  const list = el('div', 'story-pick');
  list.append(...paths.map(path => {
    const card = button('', () => readPage(path, path.start), 'book');
    card.innerHTML = FOREST.replace('class="pic"', 'class="cover"');
    const meta = el('span', 'meta');
    meta.append(el('strong', '', path.title), el('span', 'when', 'You choose'));
    card.append(meta);
    return card;
  }));
  area.append(list);
}
function readPage(path, key) {
  const area = panel(path.title), page = path.pages[key];
  area.append(el('p', 'path-text', page.text));
  if (!page.choices.length) {
    const end = el('div');
    area.append(end);
    finish(end, 'The end.', () => readPage(path, path.start), 'Read it again');
    return;
  }
  const choices = el('div', 'choices');
  choices.append(...page.choices.map(choice => button(choice.label, () => readPage(path, choice.to), 'choice')));
  area.append(choices);
}

/* ---------- 4. story pairs: a memory game, pictures and their words, no timer ---------- */
const PAIRS = [['f-fox', 'fox'], ['f-owl', 'owl'], ['f-cat', 'cat'], ['f-puppy', 'puppy'], ['f-penguin', 'penguin'],
  ['f-robot', 'robot'], ['f-unicorn', 'unicorn'], ['f-dragon', 'dragon']];
function pairs() {
  const area = panel('Story pairs');
  const count = el('p', 'hint', 'Turn over two cards. Find each picture and its word.');
  count.setAttribute('role', 'status');
  const grid = el('div', 'pairs');
  const cards = shuffle(shuffle(PAIRS).slice(0, 6).flatMap(([symbol, word]) => [{word, symbol}, {word}]));
  let open = [], found = 0, busy = false;
  const turn = (card, b) => {
    if (busy || b.classList.contains('up')) return;
    b.classList.add('up');
    b.setAttribute('aria-label', card.symbol ? `Picture of a ${card.word}` : `The word ${card.word}`);
    open.push(b);
    if (open.length < 2) return;
    const [first, second] = open;
    open = [];
    if (first.dataset.pair === second.dataset.pair) {
      for (const match of [first, second]) { match.classList.add('matched'); match.disabled = true; }
      found++;
      count.textContent = `${found} of 6 pairs found.`;
      if (found === 6) setTimeout(() => finish(area, 'All six pairs found!', pairs), 700);
      return;
    }
    busy = true;
    setTimeout(() => {
      for (const miss of [first, second]) { miss.classList.remove('up'); miss.setAttribute('aria-label', 'A card, face down'); }
      busy = false;
    }, 1100);
  };
  cards.forEach(card => {
    const b = button('', () => turn(card, b), 'card');
    b.dataset.pair = card.word;
    b.setAttribute('aria-label', 'A card, face down');
    b.innerHTML = '<span class="back" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-star"/></svg></span>';
    const face = el('span', 'face', card.symbol ? undefined : card.word);
    face.setAttribute('aria-hidden', 'true');
    if (card.symbol) face.innerHTML = picture(card.symbol);
    b.append(face);
    grid.append(b);
  });
  area.append(count, grid);
}

/* ---------- 5. draw your hero: finger drawing, the last six kept on this iPad ---------- */
const COLOURS = [['#f2c472', 'gold'], ['#e98a7a', 'coral'], ['#8fc4e8', 'sky blue'], ['#9fd6a8', 'leaf green'], ['#c3a6e8', 'lavender'], ['#f4ecd2', 'cream']];
const DRAWINGS = 'storytime.drawings';
function savedDrawings() {
  try { const list = JSON.parse(localStorage.getItem(DRAWINGS) || '[]'); return Array.isArray(list) ? list.filter(d => typeof d === 'string' && d.startsWith('data:image/jpeg')) : []; }
  catch { return []; }
}
function draw() {
  const area = panel('Draw your hero');
  area.append(el('p', 'hint', 'Who is your hero? What do they carry? Draw them with your finger.'));
  const canvas = el('canvas', 'canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Your drawing');
  const tools = el('div', 'draw-tools'), gallery = el('div', 'gallery');
  area.append(canvas, tools);
  let colour = COLOURS[0][0], last = null;
  for (const [value, name] of COLOURS) {
    const swatch = button('', () => {
      colour = value;
      tools.querySelectorAll('.swatch').forEach(other => other.setAttribute('aria-pressed', String(other === swatch)));
    }, 'swatch');
    swatch.style.background = value;
    swatch.setAttribute('aria-label', name);
    swatch.setAttribute('aria-pressed', String(value === colour));
    tools.append(swatch);
  }
  const ratio = window.devicePixelRatio || 1, width = canvas.clientWidth, height = Math.round(width * .56);
  canvas.width = width * ratio; canvas.height = height * ratio; canvas.style.height = `${height}px`;
  const pen = canvas.getContext('2d');
  pen.scale(ratio, ratio);
  const paper = () => { pen.fillStyle = '#182029'; pen.fillRect(0, 0, width, height); keep.disabled = true; };
  const point = event => { const box = canvas.getBoundingClientRect(); return {x: event.clientX - box.left, y: event.clientY - box.top}; };
  const line = (from, to) => {
    Object.assign(pen, {strokeStyle: colour, lineWidth: 10, lineCap: 'round', lineJoin: 'round'});
    pen.beginPath(); pen.moveTo(from.x, from.y); pen.lineTo(to.x, to.y); pen.stroke();
    keep.disabled = false;
  };
  canvas.addEventListener('pointerdown', event => { canvas.setPointerCapture(event.pointerId); last = point(event); line(last, last); });
  canvas.addEventListener('pointermove', event => { if (last) { const next = point(event); line(last, next); last = next; } });
  for (const type of ['pointerup', 'pointercancel']) canvas.addEventListener(type, () => { last = null; });
  const showGallery = () => {
    const drawings = savedDrawings();
    gallery.replaceChildren(...drawings.map((src, i) => { const img = el('img'); img.src = src; img.alt = `Your hero, drawing ${i + 1}`; return img; }));
    if (!drawings.length) gallery.append(el('p', 'empty', 'Your kept drawings show here.'));
  };
  const keep = button('Keep it', () => {
    // A small copy is enough for the gallery and keeps six drawings well inside the browser's storage.
    const small = el('canvas');
    small.width = 480; small.height = Math.round(480 * height / width);
    small.getContext('2d').drawImage(canvas, 0, 0, small.width, small.height);
    try {
      localStorage.setItem(DRAWINGS, JSON.stringify([...savedDrawings(), small.toDataURL('image/jpeg', .8)].slice(-6)));
      status.textContent = 'Kept! Your hero is in your gallery.';
      paper();
    } catch { status.textContent = 'This drawing could not be kept on this iPad.'; }
    showGallery();
  }, 'make');
  keep.id = 'draw-keep';
  const row = el('div', 'learn-row');
  row.append(keep, button('Start again', paper));
  area.append(row, el('h3', 'gallery-h', 'Your heroes'), gallery);
  paper();
  showGallery();
}

window.StorytimeLearn = {menu, quiz};
menu();
})();
