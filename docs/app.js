async function loadData() {
  const res = await fetch('data/articles.json', { cache: 'no-store' });
  return await res.json();
}

function formatDate(d) {
  try {
    const dt = new Date(d);
    return dt.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch { return d; }
}

/* ----- NEW: skeleton while loading ----- */
function renderSkeleton(count = 6) {
  const list = document.getElementById('list');
  list.innerHTML = '';
  for (let i = 0; i < count; i++) {
    const card = document.createElement('div');
    card.className = 'skel';
    card.innerHTML = `
      <div class="bar title"></div>
      <div class="bar meta"></div>
      <div class="bar text"></div>
      <div class="bar text"></div>
      <div class="bar text short"></div>
    `;
    list.appendChild(card);
  }
}

/* ----- render articles ----- */
function render(articles, filter = '') {
  const list = document.getElementById('list');
  list.innerHTML = '';
  const q = (filter || '').trim().toLowerCase();

  for (const a of articles) {
    if (q && !(a.title_en?.toLowerCase().includes(q) || a.summary_en?.toLowerCase().includes(q))) continue;

    const div = document.createElement('div');
    div.className = 'card';

    // Prefer local translated page when available
    const href = a.local_url ? a.local_url : a.url;
    const badgeText = a.local_url ? 'translated page' : 'translation pending';
    const badge = `<span class="chip">${badgeText}</span>`;

    div.innerHTML = `
      <a href="${href}" target="_blank" rel="noopener noreferrer">${a.title_en || a.title_it}</a>${badge}
      <div class="date">${formatDate(a.published)}</div>
      ${a.summary_en ? `<p>${a.summary_en}</p>` : ''}
    `;

    // Make the whole card clickable (fallback to main link)
    const link = div.querySelector('a');
    if (link) {
      div.addEventListener('click', (e) => {
        if (e.target.tagName.toLowerCase() === 'a') return;
        if (link.target === '_blank') {
          window.open(link.href, '_blank', 'noopener,noreferrer');
        } else {
          window.location.href = link.href;
        }
      });
    }

    list.appendChild(div);
  }

  if (!list.childNodes.length) {
    const empty = document.createElement('div');
    empty.className = 'card';
    empty.textContent = 'No articles match your search.';
    list.appendChild(empty);
  }
}

/* ----- NEW: sticky header compress on scroll ----- */
function setupStickyHeader() {
  const header = document.querySelector('header');
  if (!header) return;

  let ticking = false;
  function onScroll() {
    if (!ticking) {
      window.requestAnimationFrame(() => {
        const scrolled = window.scrollY > 8;
        header.classList.toggle('scrolled', scrolled);
        ticking = false;
      });
      ticking = true;
    }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll(); // init
}

(async () => {
  setupStickyHeader();

  // show loading placeholders
  renderSkeleton(8);

  try {
    const data = await loadData();
    document.getElementById('last-updated').textContent =
      `Last updated: ${data.generated_utc ? new Date(data.generated_utc).toLocaleString() : '—'}`;

    render(data.articles || []);

    const search = document.getElementById('search');
    search.addEventListener('input', e => render(data.articles || [], e.target.value));
  } catch (e) {
    const list = document.getElementById('list');
    list.innerHTML = '';
    const err = document.createElement('div');
    err.className = 'card';
    err.textContent = 'Failed to load articles. Please refresh.';
    list.appendChild(err);
    console.error(e);
  }
})();
