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

function render(articles, filter = '') {
  const list = document.getElementById('list');
  list.innerHTML = '';
  const q = filter.trim().toLowerCase();

  for (const a of articles) {
    if (q && !(a.title_en?.toLowerCase().includes(q) || a.summary_en?.toLowerCase().includes(q))) continue;

    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `
      <a href="${a.url}" target="_blank" rel="noopener noreferrer">${a.title_en || a.title_it}</a>
      <div class="date">${formatDate(a.published)}</div>
      ${a.summary_en ? `<p>${a.summary_en}</p>` : ''}
    `;
    list.appendChild(div);
  }

  if (!list.childNodes.length) {
    const empty = document.createElement('div');
    empty.className = 'card';
    empty.textContent = 'No articles match your search.';
    list.appendChild(empty);
  }
}

(async () => {
  const data = await loadData();
  document.getElementById('last-updated').textContent =
    `Last updated: ${data.generated_utc ? new Date(data.generated_utc).toLocaleString() : '—'}`;

  render(data.articles || []);

  const search = document.getElementById('search');
  search.addEventListener('input', e => render(data.articles || [], e.target.value));
})();
