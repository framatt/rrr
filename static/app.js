const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const itemCache = new Map();
const state = {
  view: "for-you",
  renderToken: 0,
  page: 1,
  pages: 0,
  loading: false,
  q: "",
  type: "",
  genre: "",
  age: "",
  sort: "popular",
  decade: "",
  minRating: "",
  maxRuntime: "",
  maxAge: "",
  maxAgeInitialized: false,
  kids: localStorage.getItem("pm.kids") === "1",
  facets: null,
  forYou: null,
  library: null,
  lists: null,
  list: null,
  currentDetails: null,
  detailsItem: null,
  player: null,
  streamIndex: -1,
  triedFallback: false,
  lastProgressAt: 0,
  settingsTimer: null,
  castTimer: null,
  sourcesContext: null,
  sourcesData: null,
  sourcesMode: "title",
  sourcesReturnPlayer: false,
  sourcesLoading: false,
  sourcesAbort: null,
  torrentTimer: null,
  torrentStartedAt: 0,
  torrentSeekingAt: 0,
  torrentPromptToken: "",
  torrentDismissed: {},
  downloads: null,
  downloadsTimer: null,
  downloadsLoading: false,
};

const grid = $("#browse-grid");
const catalogMeta = $("#browse-meta");
const loadMore = $("#load-more");
const searchForm = $("#search-form");
const searchInput = $("#search-input");
const detailsModal = $("#details-modal");
const detailsBody = $("#details-body");
const playerModal = $("#player-modal");
const playerTitle = $("#player-title");
const playerStatus = $("#player-status");
const video = $("#video");
const quality = $("#quality");
const playerNote = $("#player-note");
const toast = $("#toast");
const randomModal = $("#random-modal");
const settingsModal = $("#settings-modal");
const castBtn = $("#cast-btn");
const castMenu = $("#cast-menu");
const sourcesBtn = $("#sources-btn");
const sourcesModal = $("#sources-modal");
const sourcesTitle = $("#sources-title");
const sourcesSub = $("#sources-sub");
const sourcesSearchForm = $("#sources-search-form");
const sourcesQuery = $("#sources-query");
const sourcesType = $("#sources-type");
const sourcesSeason = $("#sources-season");
const sourcesEpisode = $("#sources-episode");
const sourcesQuality = $("#sources-quality");
const sourcesSort = $("#sources-sort");
const sourcesWebOnly = $("#sources-webonly");
const sourcesProviders = $("#sources-providers");
const sourcesList = $("#sources-list");
const sourcesNote = $("#sources-note");
const sourcesRefresh = $("#sources-refresh");
const slowPrompt = $("#slow-prompt");
const slowPromptText = $("#slow-prompt-text");
const slowDownloadButton = $("#slow-download");
const slowDismissButton = $("#slow-dismiss");
const downloadsList = $("#downloads-list");
const downloadsMeta = $("#downloads-meta");
const downloadsRefresh = $("#downloads-refresh");

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function itemId(item) {
  return `${String(item.type || "").toLowerCase()}:${item.tmdbId}`;
}

function libraryPayload(item) {
  return {
    type: item.type,
    tmdbId: item.tmdbId,
    title: item.title || "",
    year: item.year || "",
    poster: item.posterRaw || item.poster || "",
    backdrop: item.backdropRaw || item.backdrop || "",
  };
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function clearSearch() {
  state.q = "";
  searchInput.value = "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok || (data && data.ok === false)) {
    const error = new Error((data && data.error) || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function filterParams(extra = {}) {
  const params = new URLSearchParams();
  if (state.kids) params.set("kids", "1");
  if (state.q) params.set("q", state.q);
  if (state.type) params.set("type", state.type);
  if (state.genre) params.set("genre", state.genre);
  if (state.age) params.set("age", state.age);
  if (state.sort) params.set("sort", state.sort);
  if (state.decade) params.set("decade", state.decade);
  if (state.minRating) params.set("minRating", state.minRating);
  if (state.maxRuntime) params.set("maxRuntime", state.maxRuntime);
  if (state.maxAge) params.set("maxAge", state.maxAge);
  for (const [key, value] of Object.entries(extra)) {
    if (value === "" || value == null) params.delete(key);
    else params.set(key, value);
  }
  return params;
}

function ageText(item) {
  if (item.certification) return item.certification;
  if (item.age != null) return `${item.age}+`;
  return "";
}

function formatRuntime(minutes) {
  if (!minutes) return "";
  const value = Number(minutes);
  if (!Number.isFinite(value)) return "";
  const hours = Math.floor(value / 60);
  const rest = value % 60;
  return hours ? `${hours}h ${rest ? `${rest}m` : ""}`.trim() : `${value}m`;
}

function posterMarkup(item, className = "poster") {
  if (item.poster) {
    return `<img class="${className}" src="${esc(item.poster)}" alt="" loading="lazy" decoding="async">`;
  }
  return `<div class="poster-fallback">${esc((item.title || "?").slice(0, 1))}</div>`;
}

function cacheItems(items) {
  for (const item of items || []) {
    if (item && item.tmdbId && item.type) itemCache.set(itemId(item), item);
  }
}

function progressPercent(item) {
  const resume = item.resume || item.progress;
  if (!resume || !resume.duration) return 0;
  return Math.max(0, Math.min(100, (Number(resume.position) / Number(resume.duration)) * 100));
}

function cardMarkup(item, options = {}) {
  const id = itemId(item);
  itemCache.set(id, item);
  const year = item.year ? esc(item.year) : "";
  const rating = item.rating ? `<span class="badge rating">★ ${esc(Number(item.rating).toFixed(1))}</span>` : "";
  const type = item.type === "tv" ? "Series" : "Movie";
  const age = ageText(item);
  const ageBadge = age ? `<span class="age-badge ${esc(String(item.ageBand || ""))}">${esc(age)}</span>` : "";
  const reason = options.showReason && item.reason ? `<span class="card-reason">${esc(item.reason)}</span>` : "";
  const percent = progressPercent(item);
  const progress = percent
    ? `<span class="card-progress" aria-hidden="true"><span style="width:${percent.toFixed(1)}%"></span></span>`
    : "";
  const resumeButton = item.resume || item.progress
    ? `<button class="mini play" type="button" data-act="resume" data-id="${esc(id)}" title="Resume">▶</button>`
    : "";
  return `
    <article class="card" role="button" tabindex="0" data-id="${esc(id)}" aria-label="${esc(item.title)}">
      ${posterMarkup(item)}
      ${ageBadge}
      <span class="card-quick">
        <button class="mini ${item.inWatchlist ? "on" : ""}" type="button" data-act="watch" data-id="${esc(id)}" title="${item.inWatchlist ? "Remove from watchlist" : "Add to watchlist"}" aria-label="Watchlist">${item.inWatchlist ? "✓" : "+"}</button>
        <button class="mini ${item.liked ? "on" : ""}" type="button" data-act="like" data-id="${esc(id)}" title="${item.liked ? "Remove like" : "Like"}" aria-label="Like">♥</button>
        ${resumeButton}
      </span>
      ${progress}
      <span class="card-info">
        <span class="card-title">${esc(item.title)}</span>
        <span class="card-meta"><span>${year}</span><span class="badge">${type}</span>${rating}</span>
        ${reason}
      </span>
    </article>
  `;
}

function renderCards(items, options = {}) {
  cacheItems(items);
  return (items || []).map((item) => cardMarkup(item, options)).join("");
}

function bindPosterFallbacks(root) {
  $$("img.poster, img.details-poster", root).forEach((image) => {
    image.addEventListener(
      "error",
      () => {
        const fallback = document.createElement("div");
        fallback.className = image.classList.contains("details-poster") ? "details-poster poster-fallback" : "poster-fallback";
        fallback.textContent = "?";
        image.replaceWith(fallback);
      },
      { once: true }
    );
  });
}

async function handleCardAction(action, id) {
  const item = itemCache.get(id);
  if (!item) return;
  if (action === "watch") {
    const add = !item.inWatchlist;
    item.inWatchlist = add;
    updateCardStates(id);
    try {
      await api("/api/library", {
        method: "POST",
        body: JSON.stringify({ action: add ? "watchlist-add" : "watchlist-remove", item: libraryPayload(item), key: id }),
      });
      showToast(add ? "Added to My List" : "Removed from My List");
      await loadLibrary(true);
    } catch (error) {
      item.inWatchlist = !add;
      updateCardStates(id);
      showToast(error.message);
    }
    return;
  }
  if (action === "like") {
    const add = !item.liked;
    item.liked = add;
    item.disliked = false;
    updateCardStates(id);
    try {
      await api("/api/library", { method: "POST", body: JSON.stringify({ action: add ? "like" : "clear-reaction", key: id }) });
      showToast(add ? "Liked — recommendations updated" : "Like removed");
      await loadLibrary(true);
    } catch (error) {
      item.liked = !add;
      updateCardStates(id);
      showToast(error.message);
    }
    return;
  }
  if (action === "resume") {
    playFromItem(item);
  }
}

function updateCardStates(id) {
  const item = itemCache.get(id);
  if (!item) return;
  $$(`.card[data-id="${CSS.escape(id)}"]`).forEach((card) => {
    const watch = $("[data-act='watch']", card);
    const like = $("[data-act='like']", card);
    if (watch) {
      watch.classList.toggle("on", !!item.inWatchlist);
      watch.textContent = item.inWatchlist ? "✓" : "+";
    }
    if (like) like.classList.toggle("on", !!item.liked);
  });
}

function bindCards(root) {
  $$(".card", root).forEach((card) => {
    if (card.dataset.bound === "1") return;
    card.dataset.bound = "1";
    card.addEventListener("click", (event) => {
      const action = event.target.closest("[data-act]");
      if (action) {
        event.stopPropagation();
        handleCardAction(action.dataset.act, action.dataset.id);
        return;
      }
      openDetails(card.dataset.id);
    });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDetails(card.dataset.id);
      }
    });
  });
  bindPosterFallbacks(root);
}

function syncCardCacheFlags() {
  if (!state.library) return;
  for (const [id, flags] of Object.entries(state.library.state || {})) {
    const item = itemCache.get(id);
    if (!item) continue;
    item.inWatchlist = !!flags.inWatchlist;
    item.liked = !!flags.liked;
    item.disliked = !!flags.disliked;
    item.watched = !!flags.watched;
    item.progress = flags.progress;
    updateCardStates(id);
  }
}

// -- routing ----------------------------------------------------------------

function parseHash() {
  const hash = (location.hash || "#/for-you").replace(/^#\/?/, "");
  const [view, ...rest] = hash.split("/");
  if (view === "list" && rest.length) return { view: "list", listId: decodeURIComponent(rest.join("/")) };
  if (["for-you", "browse", "lists", "library", "downloads"].includes(view)) return { view };
  return { view: "for-you" };
}

function setView(view, updateHash = true) {
  state.view = view;
  if (view !== "downloads") stopDownloadsPolling();
  $$(".view").forEach((section) => section.classList.add("hidden"));
  const target = $(`#view-${view === "list" ? "list" : view}`);
  if (target) target.classList.remove("hidden");
  $$(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.view === view || (view === "list" && button.dataset.view === "lists")));
  if (updateHash) {
    const hash = view === "list" ? `#/list/${encodeURIComponent(state.list?.id || "")}` : `#/${view}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function route() {
  const parsed = parseHash();
  state.renderToken += 1;
  if (parsed.view === "list") {
    await openList(parsed.listId, false);
    return;
  }
  setView(parsed.view, false);
  if (parsed.view === "for-you") {
    clearSearch();
    await loadForYou();
  } else if (parsed.view === "browse") await loadBrowse(true);
  else if (parsed.view === "lists") await loadLists();
  else if (parsed.view === "downloads") await loadDownloads();
  else if (parsed.view === "library") await loadLibraryView();
}

// -- library ----------------------------------------------------------------

async function loadLibrary(force = false) {
  if (state.library && !force) return state.library;
  try {
    const params = state.kids ? "?kids=1" : "";
    state.library = await api(`/api/library${params}`);
    if (!state.maxAgeInitialized) {
      const savedMaxAge = state.library?.settings?.maxAge;
      if (savedMaxAge != null && savedMaxAge !== "" && state.maxAge === "") state.maxAge = String(savedMaxAge);
      state.maxAgeInitialized = true;
    }
    syncCardCacheFlags();
  } catch (error) {
    showToast(error.message);
  }
  return state.library;
}

async function libraryAction(action, item, extra = {}) {
  const payload = { action, ...extra };
  if (item) {
    payload.item = libraryPayload(item);
    payload.key = itemId(item);
  }
  const result = await api("/api/library", { method: "POST", body: JSON.stringify(payload) });
  await loadLibrary(true);
  return result;
}

// -- for you ----------------------------------------------------------------

function heroMarkup(item) {
  const backdrop = item.backdrop ? `style="background-image:url('${esc(item.backdrop)}')"` : "";
  const meta = [item.year, item.type === "tv" ? "Series" : "Movie", item.rating ? `★ ${Number(item.rating).toFixed(1)}` : "", ageText(item), formatRuntime(item.runtime)]
    .filter(Boolean)
    .map((part) => `<span>${esc(part)}</span>`)
    .join("");
  return `
    <section class="hero" ${backdrop}>
      <div class="hero-inner">
        <div class="hero-copy">
          <span class="eyebrow">${esc(item.reason || "Top pick for you")}</span>
          <h1>${esc(item.title)}</h1>
          <div class="hero-meta">${meta}</div>
          <p>${esc(item.overview || "No overview available.")}</p>
          <div class="hero-actions">
            <button class="btn primary" type="button" data-hero-action="play">▶ Play ${item.type === "tv" ? "S1 E1" : "now"}</button>
            <button class="btn" type="button" data-hero-action="details">More info</button>
            <button class="btn ghost" type="button" data-hero-action="watch">${item.inWatchlist ? "✓ In My List" : "+ My List"}</button>
          </div>
        </div>
      </div>
    </section>
  `;
}

function railMarkup(row) {
  if (!row.items || !row.items.length) return "";
  const seeAll = row.id && row.id.startsWith("genre-")
    ? `<button class="btn ghost small" type="button" data-see-genre="${esc(row.title.replace(/^More /, ""))}">See all</button>`
    : "";
  return `
    <section class="rail">
      <div class="rail-head">
        <div><h2>${esc(row.title)}</h2><p>${esc(row.subtitle || "")}</p></div>
        ${seeAll}
      </div>
      <div class="rail-scroll">${renderCards(row.items, { showReason: true })}</div>
    </section>
  `;
}

async function loadForYou() {
  const token = state.renderToken;
  const root = $("#for-you-rows");
  const heroRoot = $("#for-you-hero");
  heroRoot.innerHTML = `<div class="skeleton hero-skeleton"></div>`;
  root.innerHTML = `<div class="skeleton-row">${'<div class="skeleton card-skeleton"></div>'.repeat(6)}</div>`;
  try {
    const data = await api(`/api/recommendations?${filterParams({ limit: "18" })}`);
    if (token !== state.renderToken) return;
    state.forYou = data;
    const rows = (data.rows || []).filter((row) => row.items && row.items.length);
    cacheItems(rows.flatMap((row) => row.items));
    syncCardCacheFlags();
    const featured = (rows.find((row) => row.id === "top-picks") || rows.find((row) => row.id !== "continue") || rows[0] || {}).items?.[0];
    heroRoot.innerHTML = featured ? heroMarkup(featured) : "";
    if (featured) {
      const hero = $(".hero", heroRoot);
      $$("[data-hero-action]", hero).forEach((button) => {
        button.addEventListener("click", () => {
          if (button.dataset.heroAction === "play") playFromItem(featured);
          else if (button.dataset.heroAction === "details") openDetails(itemId(featured));
          else handleCardAction("watch", itemId(featured));
        });
      });
    }
    root.innerHTML = rows.map(railMarkup).join("") || `<p class="empty">No recommendations yet. Browse a few titles and like what you enjoy.</p>`;
    bindCards(root);
    $$("[data-see-genre]", root).forEach((button) => {
      button.addEventListener("click", () => {
        state.genre = button.dataset.seeGenre;
        setView("browse");
        loadBrowse(true);
      });
    });
    const profile = data.profile || {};
    if (profile.personalised && profile.topGenres?.length) {
      catalogMeta.textContent = `Tuned to: ${profile.topGenres.join(", ")}`;
    }
  } catch (error) {
    if (token !== state.renderToken) return;
    heroRoot.innerHTML = "";
    root.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

// -- browse -----------------------------------------------------------------

function decadeOptions() {
  const decades = (state.facets?.decades || []).map((entry) => entry.decade);
  return decades.map((decade) => `<option value="${decade}">${decade}s</option>`).join("");
}

function renderFilterBar() {
  const bar = $("#filter-bar");
  if (!bar) return;
  const genres = (state.facets?.genres || []).map((entry) => entry.name);
  if (state.genre && !genres.includes(state.genre)) genres.unshift(state.genre);
  bar.innerHTML = `
    <label>Type
      <select data-filter="type">
        <option value="">All</option>
        <option value="movie" ${state.type === "movie" ? "selected" : ""}>Movies</option>
        <option value="tv" ${state.type === "tv" ? "selected" : ""}>Series</option>
      </select>
    </label>
    <label>Genre
      <select data-filter="genre">
        <option value="">All genres</option>
        ${genres.map((name) => `<option value="${esc(name)}" ${state.genre === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
      </select>
    </label>
    <label>Age
      <select data-filter="age" ${state.kids ? "disabled" : ""}>
        <option value="">Any age</option>
        <option value="kids" ${state.age === "kids" ? "selected" : ""}>Kids-safe</option>
        <option value="family" ${state.age === "family" ? "selected" : ""}>Up to 12</option>
        <option value="teen" ${state.age === "teen" ? "selected" : ""}>Up to 15</option>
        <option value="adult" ${state.age === "adult" ? "selected" : ""}>Adults only</option>
      </select>
    </label>
    <label>Sort
      <select data-filter="sort">
        <option value="popular" ${state.sort === "popular" ? "selected" : ""}>Popular</option>
        <option value="rating" ${state.sort === "rating" ? "selected" : ""}>Top rated</option>
        <option value="newest" ${state.sort === "newest" ? "selected" : ""}>Newest</option>
        <option value="oldest" ${state.sort === "oldest" ? "selected" : ""}>Oldest</option>
        <option value="hidden" ${state.sort === "hidden" ? "selected" : ""}>Hidden gems</option>
        <option value="az" ${state.sort === "az" ? "selected" : ""}>A–Z</option>
      </select>
    </label>
    <label>Decade
      <select data-filter="decade">
        <option value="">Any decade</option>
        ${decadeOptions()}
      </select>
    </label>
    <label>Rating
      <select data-filter="minRating">
        <option value="">Any rating</option>
        <option value="6" ${state.minRating === "6" ? "selected" : ""}>6+</option>
        <option value="7" ${state.minRating === "7" ? "selected" : ""}>7+</option>
        <option value="8" ${state.minRating === "8" ? "selected" : ""}>8+</option>
      </select>
    </label>
    <label>Runtime
      <select data-filter="maxRuntime">
        <option value="">Any runtime</option>
        <option value="90" ${state.maxRuntime === "90" ? "selected" : ""}>≤ 90 min</option>
        <option value="120" ${state.maxRuntime === "120" ? "selected" : ""}>≤ 2 h</option>
        <option value="150" ${state.maxRuntime === "150" ? "selected" : ""}>≤ 2.5 h</option>
        <option value="180" ${state.maxRuntime === "180" ? "selected" : ""}>≤ 3 h</option>
      </select>
    </label>
    <button id="filter-reset" class="btn ghost small" type="button">Reset</button>
  `;
  $$("[data-filter]", bar).forEach((select) => {
    select.addEventListener("change", () => {
      state[select.dataset.filter] = select.value;
      loadBrowse(true);
    });
  });
  const reset = $("#filter-reset", bar);
  if (reset) {
    reset.addEventListener("click", () => {
      state.genre = "";
      state.age = "";
      state.sort = "popular";
      state.decade = "";
      state.minRating = "";
      state.maxRuntime = "";
      state.type = "";
      state.q = "";
      searchInput.value = "";
      renderFilterBar();
      loadBrowse(true);
    });
  }
}

async function loadFacets() {
  if (state.facets) return state.facets;
  try {
    state.facets = await api("/api/facets");
  } catch (error) {
    state.facets = { genres: [], decades: [], age: {}, coverage: {} };
    showToast(error.message);
  }
  return state.facets;
}

async function loadBrowse(reset = false) {
  const token = state.renderToken;
  if (state.loading && !reset) return;
  state.loading = true;
  await loadFacets();
  if (token !== state.renderToken) {
    state.loading = false;
    return;
  }
  if (reset || !$("#filter-bar").innerHTML) renderFilterBar();
  if (reset) {
    state.page = 1;
    state.pages = 0;
    grid.innerHTML = "";
    catalogMeta.textContent = "Loading catalog…";
  }
  loadMore.classList.add("hidden");
  try {
    const params = filterParams({ page: String(state.page), limit: "60" });
    const data = await api(`/api/catalog?${params.toString()}`);
    if (token !== state.renderToken) return;
    state.pages = data.pages || 0;
    state.total = data.total || 0;
    if (reset && !(data.items || []).length) {
      grid.innerHTML = `
        <div class="empty-block">
          <p class="empty">No titles match these filters. Try resetting them${state.kids ? " or turning off kids mode" : ""}.</p>
          ${state.q && !state.kids ? `<button id="torrent-search-empty" class="btn primary" type="button">Find "${esc(state.q)}" on torrents</button>` : ""}
        </div>
      `;
      const torrentButton = $("#torrent-search-empty");
      if (torrentButton) {
        torrentButton.addEventListener("click", () => {
          openSources(
            { global: true, type: state.type || "movie", query: state.q, title: state.q },
            { search: true, autoSearch: true },
          );
        });
      }
    } else {
      grid.insertAdjacentHTML("beforeend", renderCards(data.items || []));
    }
    bindCards(grid);
    syncCardCacheFlags();
    catalogMeta.textContent = `${data.total} titles · page ${data.page} of ${Math.max(1, state.pages)}`;
    const count = $("#browse-count");
    if (count) count.textContent = state.kids ? `${data.total} kid-safe titles` : `${data.total} titles`;
    if (state.page < state.pages) loadMore.classList.remove("hidden");
  } catch (error) {
    if (token !== state.renderToken) return;
    catalogMeta.textContent = "Catalog unavailable.";
    showToast(error.message || "Could not load catalog.");
  } finally {
    state.loading = false;
  }
}

// -- lists ------------------------------------------------------------------

function listCardMarkup(record) {
  const covers = (record.samples || []).slice(0, 4).map((item) => posterMarkup(item, "list-poster")).join("");
  return `
    <article class="list-card" role="button" tabindex="0" data-list-id="${esc(record.id)}">
      <div class="list-covers">${covers || '<div class="list-poster poster-fallback">?</div>'}</div>
      <div class="list-info">
        <span class="badge source">${esc(record.source || record.kind || "List")}</span>
        <h3>${esc(record.name)}</h3>
        <p>${esc(record.count)} titles${record.subtitle ? ` · ${esc(record.subtitle)}` : ""}</p>
      </div>
    </article>
  `;
}

async function loadLists() {
  const token = state.renderToken;
  const root = $("#lists-grid");
  root.innerHTML = `<div class="skeleton-row">${'<div class="skeleton list-skeleton"></div>'.repeat(4)}</div>`;
  try {
    const data = await api(`/api/lists${state.kids ? "?kids=1" : ""}`);
    if (token !== state.renderToken) return;
    state.lists = data;
    cacheItems((data.lists || []).flatMap((record) => record.samples || []));
    root.innerHTML = (data.lists || []).map(listCardMarkup).join("") || `<p class="empty">No lists yet. Import one above.</p>`;
    $$("[data-list-id]", root).forEach((card) => {
      card.addEventListener("click", () => openList(card.dataset.listId));
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openList(card.dataset.listId);
        }
      });
    });
    bindPosterFallbacks(root);
  } catch (error) {
    if (token !== state.renderToken) return;
    root.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

async function openList(listId, updateHash = true) {
  state.renderToken += 1;
  const token = state.renderToken;
  setView("list", updateHash ? false : updateHash);
  if (updateHash) {
    const hash = `#/list/${encodeURIComponent(listId)}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
  }
  $("#list-title").textContent = "Loading list…";
  $("#list-meta").textContent = "";
  $("#list-grid").innerHTML = "";
  $("#delete-list").classList.add("hidden");
  try {
    const data = await api(`/api/lists/${encodeURIComponent(listId)}${state.kids ? "?kids=1" : ""}`);
    if (token !== state.renderToken) return;
    state.list = data.list;
    const items = (data.items || []).filter((item) => !state.kids || item.kidsSafe);
    cacheItems(items);
    syncCardCacheFlags();
    $("#list-title").textContent = data.list.name;
    $("#list-meta").textContent = `${items.length} titles · ${data.list.source || data.list.kind}${data.list.subtitle ? ` · ${data.list.subtitle}` : ""}`;
    $("#list-grid").innerHTML = renderCards(items);
    bindCards($("#list-grid"));
    if (String(data.list.id || "").startsWith("custom-")) $("#delete-list").classList.remove("hidden");
  } catch (error) {
    if (token !== state.renderToken) return;
    $("#list-title").textContent = "List unavailable";
    $("#list-meta").textContent = error.message;
  }
}

async function importList(kind) {
  const payload = {};
  if (kind === "url") {
    payload.url = $("#list-url").value.trim();
    if (!payload.url) {
      showToast("Paste a list URL first.");
      return;
    }
  } else {
    payload.text = $("#list-text").value;
    payload.name = $("#list-name").value.trim();
    if (!payload.text.trim()) {
      showToast("Paste some titles first.");
      return;
    }
  }
  const button = kind === "url" ? $("#import-url-btn") : $("#import-text-btn");
  button.disabled = true;
  button.textContent = "Importing…";
  try {
    const result = await api("/api/lists/import", { method: "POST", body: JSON.stringify(payload) });
    const missing = result.missing ? ` ${result.missing} unmatched title(s) skipped.` : "";
    showToast(`Imported “${result.list.name}” — ${result.list.count} matched titles.${missing}`);
    $("#list-url").value = "";
    $("#list-text").value = "";
    $("#list-name").value = "";
    await loadLists();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = kind === "url" ? "Import URL" : "Import pasted list";
  }
}

// -- library view -----------------------------------------------------------

function libraryRail(title, items, subtitle = "", options = {}) {
  if (!items || !items.length) return "";
  return `
    <section class="rail">
      <div class="rail-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div></div>
      <div class="rail-scroll">${renderCards(items, options)}</div>
    </section>
  `;
}

async function loadLibraryView() {
  const token = state.renderToken;
  const root = $("#library-rows");
  root.innerHTML = `<div class="skeleton-row">${'<div class="skeleton card-skeleton"></div>'.repeat(6)}</div>`;
  try {
    const data = await api(`/api/library${state.kids ? "?kids=1" : ""}`);
    if (token !== state.renderToken) return;
    state.library = data;
    syncCardCacheFlags();
    $("#library-stats").textContent = `${data.stats.watchlist} on your list · ${data.stats.watched} watched · ${data.stats.liked} liked · ${data.stats.progress} in progress`;
    const rows = [
      libraryRail("Continue watching", data.progress, "Pick up where you stopped.", { showReason: false }),
      libraryRail("My List", data.watchlist, "Everything you saved for later."),
      libraryRail("Liked", data.liked, "These shape your recommendations."),
      libraryRail("Watched", data.watched, "Already seen."),
      libraryRail("Recently opened", data.history, "Your latest activity."),
      data.lists?.length
        ? `<section class="rail"><div class="rail-head"><div><h2>Imported lists</h2><p>Lists you brought in from the internet.</p></div></div><div class="list-grid compact">${data.lists.map(listCardMarkup).join("")}</div></section>`
        : "",
    ];
    root.innerHTML = rows.join("") || `<p class="empty">Your library is empty. Add titles with the + button on any poster.</p>`;
    bindCards(root);
    $$("[data-list-id]", root).forEach((card) => {
      card.addEventListener("click", () => openList(card.dataset.listId));
    });
  } catch (error) {
    if (token !== state.renderToken) return;
    root.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

// -- downloads view ---------------------------------------------------------

function stopDownloadsPolling() {
  clearTimeout(state.downloadsTimer);
  state.downloadsTimer = null;
}

function scheduleDownloadsPolling() {
  stopDownloadsPolling();
  if (state.view !== "downloads") return;
  state.downloadsTimer = setTimeout(() => {
    if (state.view === "downloads") loadDownloads();
  }, 5000);
}

async function loadDownloads() {
  if (state.downloadsLoading) return;
  state.downloadsLoading = true;
  try {
    const data = await api("/api/downloads");
    state.downloads = data;
    renderDownloads(data);
  } catch (error) {
    state.downloads = null;
    downloadsList.innerHTML = `<p class="empty">${esc(error.message || "Could not load downloads.")}</p>`;
  } finally {
    state.downloadsLoading = false;
    scheduleDownloadsPolling();
  }
}

function downloadRowMarkup(record) {
  const status = String(record.status || "");
  const missing = status === "done" && record.local === false;
  const done = status === "done" && !missing;
  const failed = status === "error" || missing;
  const playable = done || (record.playable && !missing);
  const percent = Math.max(0, Math.min(100, Number(record.progress || 0) * 100));
  const size = Number(record.size || 0);
  const downloaded = Number(record.downloaded || 0);
  const speed = Number(record.downloadSpeed || 0);
  const peers = Number(record.numPeers || 0);
  const eta = Number(record.timeRemaining || 0);
  const title = record.displayTitle || record.title || record.fileName || "Download";
  const poster = record.poster
    ? `<img class="download-poster" src="${esc(record.poster)}" alt="" loading="lazy" decoding="async">`
    : `<div class="download-poster poster-fallback">${esc(String(title).slice(0, 1) || "?")}</div>`;
  const bits = [];
  if (done) {
    bits.push("Ready to watch");
    if (size) bits.push(formatBytes(size));
  } else if (failed) {
    bits.push(missing ? "The downloaded file is missing. Retry to fetch it again." : record.error || "This download needs attention.");
  } else {
    bits.push(`${percent.toFixed(percent >= 10 ? 0 : 1)}%`);
    if (size) bits.push(`${formatBytes(downloaded)} of ${formatBytes(size)}`);
    if (speed) bits.push(`${formatBytes(speed)}/s`);
    bits.push(`${peers} peer${peers === 1 ? "" : "s"}`);
    if (eta > 0 && Number.isFinite(eta)) bits.push(`${formatEta(eta)} left`);
    if (record.error) bits.push(record.error);
  }
  const bar = done || failed
    ? ""
    : `<span class="download-bar" aria-hidden="true"><span style="width:${percent}%"></span></span>`;
  const retry = failed || (!done && record.error)
    ? `<button class="btn ghost small" type="button" data-download-retry="${esc(record.id)}">Retry</button>`
    : "";
  return `
    <article class="download-row">
      ${poster}
      <div class="download-main">
        <p class="download-title" title="${esc(title)}">${esc(title)}</p>
        <p class="download-meta">${esc(bits.join(" · "))}</p>
        ${record.fileName ? `<p class="download-file" title="${esc(record.fileName)}">${esc(record.fileName)}</p>` : ""}
        ${bar}
      </div>
      <div class="download-actions">
        <button class="btn primary small" type="button" data-download-play="${esc(record.id)}" ${playable ? "" : "disabled"}>${done ? "Play" : "Watch now"}</button>
        ${retry}
        <button class="btn danger small" type="button" data-download-remove="${esc(record.id)}">Remove</button>
      </div>
    </article>
  `;
}

function renderDownloads(data) {
  const items = data.downloads || [];
  const stats = data.stats || {};
  if (!items.length) {
    downloadsMeta.textContent = "Torrents kept downloading in the background so you can watch them later.";
    downloadsList.innerHTML = `
      <div class="empty-block">
        <p class="empty">No downloads yet.</p>
        <p class="hint">When a torrent streams too slowly, choose “Add to Downloads” in the player and it will keep downloading here.</p>
      </div>`;
    return;
  }
  const parts = [];
  if (stats.downloading) parts.push(`${stats.downloading} downloading`);
  if (stats.done) parts.push(`${stats.done} ready to watch`);
  if (stats.error) parts.push(`${stats.error} needs attention`);
  downloadsMeta.textContent = parts.length
    ? `${parts.join(" · ")}. Downloads continue while this server is running.`
    : "Torrents kept downloading in the background so you can watch them later.";
  downloadsList.innerHTML = items.map(downloadRowMarkup).join("");
  $$("[data-download-play]", downloadsList).forEach((button) => {
    button.addEventListener("click", () => playDownload(button.dataset.downloadPlay, button));
  });
  $$("[data-download-retry]", downloadsList).forEach((button) => {
    button.addEventListener("click", () => retryDownload(button.dataset.downloadRetry, button));
  });
  $$("[data-download-remove]", downloadsList).forEach((button) => {
    button.addEventListener("click", () => removeDownload(button.dataset.downloadRemove, button));
  });
}

async function playDownload(id, button) {
  if (!id) return;
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Opening…";
  }
  try {
    const data = await api("/api/downloads/play", { method: "POST", body: JSON.stringify({ id }) });
    openDownloadPlayer(data);
  } catch (error) {
    showToast(error.message || "Could not open this download.");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function openDownloadPlayer(data) {
  const context = data.context || {};
  const item = context.item && context.item.tmdbId
    ? context.item
    : {
        type: context.type || "movie",
        tmdbId: context.tmdbId || "",
        title: context.title || "Download",
        year: context.year || "",
        poster: data.download?.poster || "",
        backdrop: data.download?.backdrop || "",
      };
  openTorrentPlayer({ ...context, item });
  if (data.resume) state.player.resume = data.resume;
  const stream = {
    ...(data.stream || {}),
    torrent: data.stream?.kind === "torrent",
    keep: true,
  };
  const streams = state.player.streams || [];
  const existing = streams.findIndex(
    (entry) => (stream.url && entry.url === stream.url) || (stream.token && entry.token === stream.token),
  );
  if (existing >= 0) streams[existing] = stream;
  else streams.push(stream);
  state.player.streams = streams;
  renderQuality(streams);
  setStream(existing >= 0 ? existing : streams.length - 1);
}

async function retryDownload(id, button) {
  if (!id) return;
  if (button) button.disabled = true;
  try {
    await api("/api/downloads/retry", { method: "POST", body: JSON.stringify({ id }) });
    showToast("Retrying download…");
    await loadDownloads();
  } catch (error) {
    showToast(error.message || "Could not retry this download.");
    if (button) button.disabled = false;
  }
}

async function removeDownload(id, button) {
  if (!id) return;
  if (!confirm("Remove this download? Any downloaded file will be deleted.")) return;
  if (button) button.disabled = true;
  try {
    await api("/api/downloads/remove", {
      method: "POST",
      body: JSON.stringify({ id, deleteFiles: true }),
    });
    showToast("Download removed.");
    await loadDownloads();
  } catch (error) {
    showToast(error.message || "Could not remove this download.");
    if (button) button.disabled = false;
  }
}

async function openDetails(idOrItem) {
  const id = typeof idOrItem === "string" ? idOrItem : itemId(idOrItem);
  let item = typeof idOrItem === "string" ? itemCache.get(idOrItem) : idOrItem;
  if (!item) return;
  randomModal.classList.add("hidden");
  settingsModal.classList.add("hidden");
  clearInterval(state.settingsTimer);
  state.detailsItem = item;
  detailsModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  detailsBody.innerHTML = `<div class="details-body"><p class="details-overview">Fetching details…</p></div>`;
  try {
    const data = await api(`/api/details?type=${encodeURIComponent(item.type)}&id=${encodeURIComponent(item.tmdbId)}`);
    state.currentDetails = data;
    applyDetailFlags(data);
    renderDetails(data);
  } catch (error) {
    detailsBody.innerHTML = `<div class="details-body"><p class="details-overview">${esc(error.message)}</p></div>`;
  }
}

function applyDetailFlags(data) {
  const flags = data.library || {};
  const item = state.detailsItem || {};
  item.inWatchlist = !!flags.inWatchlist;
  item.liked = !!flags.liked;
  item.disliked = !!flags.disliked;
  item.watched = !!flags.watched;
  item.resume = flags.progress || null;
  if (data.poster) item.poster = data.poster;
  if (data.backdrop) item.backdrop = data.backdrop;
  state.detailsItem = item;
  itemCache.set(itemId(item), item);
}

function detailButtonsMarkup(data) {
  const flags = data.library || {};
  return `
    <div class="details-actions">
      <button class="btn primary" id="detail-play" type="button">▶ ${data.type === "tv" ? "Play S1 E1" : "Play now"}</button>
      ${data.progressEntries?.length ? `<button class="btn" id="detail-continue" type="button">▶ Continue S${esc(data.progressEntries[0].season)} E${esc(data.progressEntries[0].episode)}</button>` : ""}
      <button class="btn ${flags.inWatchlist ? "active" : ""}" id="detail-watch" type="button">${flags.inWatchlist ? "✓ In My List" : "+ My List"}</button>
      <button class="btn ${flags.liked ? "active" : ""}" id="detail-like" type="button">♥ Like</button>
      <button class="btn ${flags.disliked ? "active" : ""}" id="detail-dislike" type="button">✕ Not for me</button>
      <button class="btn ${flags.watched ? "active" : ""}" id="detail-watched" type="button">${flags.watched ? "✓ Watched" : "Mark watched"}</button>
      ${data.trailerUrl ? `<a class="btn ghost" href="${esc(data.trailerUrl)}" target="_blank" rel="noopener noreferrer">▶ Trailer</a>` : ""}
    </div>
  `;
}

function renderDetails(data) {
  const poster = data.poster ? `<img class="details-poster" src="${esc(data.poster)}" alt="">` : `<div class="details-poster poster-fallback">${esc((data.title || "?").slice(0, 1))}</div>`;
  const age = ageText({ certification: data.certification?.rating, age: data.age });
  const meta = [
    data.year ? esc(data.year) : "",
    data.type === "tv" ? "Series" : "Movie",
    data.rating ? `★ ${esc(Number(data.rating).toFixed(1))}` : "",
    data.runtime ? esc(formatRuntime(data.runtime)) : "",
    ...(data.genres || []).slice(0, 4).map((genre) => esc(genre.name)),
  ].filter(Boolean).join(" · ");
  const heroStyle = data.backdrop ? `style="background-image:url('${esc(data.backdrop)}')"` : "";
  const cast = (data.cast || []).slice(0, 8).map((person) => `<span class="cast-chip">${esc(person.name)}<small>${esc(person.character || "")}</small></span>`).join("");
  detailsBody.innerHTML = `
    <div class="details-hero" ${heroStyle}>
      <div class="details-content">
        ${poster}
        <div class="details-main">
          <div class="details-topline">${age ? `<span class="age-badge ${esc(data.ageBand || "")}">${esc(age)}</span>` : ""}${data.kidsSafe ? `<span class="badge kid">Kids-safe</span>` : ""}</div>
          <h2 id="details-title">${esc(data.title || state.detailsItem?.title || "")}</h2>
          <div class="details-meta">${meta}</div>
          ${data.tagline ? `<p class="tagline">“${esc(data.tagline)}”</p>` : ""}
          <p class="details-overview">${esc(data.overview || "No overview available.")}</p>
          ${detailButtonsMarkup(data)}
        </div>
      </div>
    </div>
    <div class="details-body" id="details-lower">
      ${cast ? `<h3 class="section-label">Cast</h3><div class="cast-row">${cast}</div>` : ""}
      ${data.type === "tv" ? `<div id="season-area"></div>` : ""}
      ${data.similar?.length ? `<section class="rail details-rail"><div class="rail-head"><div><h2>More like this</h2><p>Matched by genre and viewer similarity.</p></div></div><div class="rail-scroll">${renderCards(data.similar, { showReason: true })}</div></section>` : ""}
    </div>
  `;
  bindPosterFallbacks(detailsBody);
  const item = state.detailsItem;
  $("#detail-play", detailsBody).addEventListener("click", () => {
    playTitle(data.type, item.tmdbId, data.title || item.title, data.initialSeason || 1, 1, item);
  });
  const continueButton = $("#detail-continue", detailsBody);
  if (continueButton) {
    continueButton.addEventListener("click", () => {
      const entry = data.progressEntries[0];
      playTitle(data.type, item.tmdbId, `${data.title} · S${entry.season} E${entry.episode}`, entry.season, entry.episode, item);
    });
  }
  bindDetailAction("#detail-watch", "watch", item, data);
  bindDetailAction("#detail-like", "like", item, data);
  bindDetailAction("#detail-dislike", "dislike", item, data);
  bindDetailAction("#detail-watched", "watched", item, data);
  if (data.type === "tv") renderSeasonPicker(data, item);
  bindCards(detailsBody);
}

function bindDetailAction(selector, action, item, data) {
  const button = $(selector, detailsBody);
  if (!button) return;
  button.addEventListener("click", async () => {
    try {
      if (action === "watch") {
        const add = !(data.library || {}).inWatchlist;
        await libraryAction(add ? "watchlist-add" : "watchlist-remove", item);
        data.library.inWatchlist = add;
        item.inWatchlist = add;
      } else if (action === "like") {
        const add = !(data.library || {}).liked;
        await libraryAction(add ? "like" : "clear-reaction", item);
        data.library.liked = add;
        data.library.disliked = false;
        item.liked = add;
        item.disliked = false;
      } else if (action === "dislike") {
        const add = !(data.library || {}).disliked;
        await libraryAction(add ? "dislike" : "clear-reaction", item);
        data.library.disliked = add;
        data.library.liked = false;
        item.disliked = add;
        item.liked = false;
      } else if (action === "watched") {
        const add = !(data.library || {}).watched;
        await libraryAction(add ? "watched" : "unwatched", item);
        data.library.watched = add;
        item.watched = add;
      }
      renderDetails(data);
    } catch (error) {
      showToast(error.message);
    }
  });
}

function renderSeasonPicker(data, item) {
  const area = $("#season-area", detailsBody);
  if (!area) return;
  const seasons = (data.seasons || []).filter((season) => season && season.season_number);
  const initial = data.initialSeason || (seasons[0] && seasons[0].season_number) || 1;
  const options = (seasons.length ? seasons : [{ season_number: 1, episode_count: (data.initialEpisodes || []).length }])
    .map((season) => `<option value="${esc(season.season_number)}" ${Number(season.season_number) === Number(initial) ? "selected" : ""}>${esc(season.name || `Season ${season.season_number}`)} (${esc(season.episode_count ?? "")})</option>`)
    .join("");
  area.innerHTML = `
    <div class="season-row"><label for="season-select">Season</label><select id="season-select">${options}</select></div>
    <div id="episode-list" class="episode-list"></div>
  `;
  renderEpisodes(data.initialEpisodes || [], initial, item);
  $("#season-select", area).addEventListener("change", async (event) => {
    const season = event.target.value;
    const list = $("#episode-list", area);
    list.innerHTML = `<p class="player-note">Loading episodes…</p>`;
    try {
      const result = await api(`/api/season?tmdbId=${encodeURIComponent(item.tmdbId)}&season=${encodeURIComponent(season)}`);
      renderEpisodes(result.episodes || [], season, item);
    } catch (error) {
      list.innerHTML = `<p class="player-note">${esc(error.message)}</p>`;
    }
  });
}

function renderEpisodes(episodes, season, item) {
  const list = $("#episode-list", detailsBody);
  if (!list) return;
  if (!episodes.length) {
    list.innerHTML = `<p class="player-note">No episodes found.</p>`;
    return;
  }
  list.innerHTML = episodes.map((episode) => {
    const number = episode.episode ?? episode.ep ?? "";
    const name = episode.name || `Episode ${number}`;
    const sub = [episode.runtime ? `${episode.runtime}m` : "", episode.airDate || ""].filter(Boolean).join(" · ");
    return `
      <button class="episode" type="button" data-season="${esc(season)}" data-episode="${esc(number)}" data-name="${esc(name)}">
        <span class="episode-num">E${esc(number)}</span>
        <span><span class="episode-name">${esc(name)}</span><span class="episode-sub">${esc(sub)}</span></span>
        <span class="episode-play">▶</span>
      </button>
    `;
  }).join("");
  $$(".episode", list).forEach((button) => {
    button.addEventListener("click", () => {
      playTitle("tv", item.tmdbId, `${item.title} · S${button.dataset.season} E${button.dataset.episode}`, Number(button.dataset.season), Number(button.dataset.episode), item);
    });
  });
}

function closeDetails() {
  detailsModal.classList.add("hidden");
  if (playerModal.classList.contains("hidden")) document.body.style.overflow = "";
  detailsBody.innerHTML = "";
  state.currentDetails = null;
}

// -- player -----------------------------------------------------------------

function playFromItem(item) {
  if (!item) return;
  if (state.kids && !item.kidsSafe) {
    showToast("Kids mode is on — this title is outside the kid-safe list.");
    return;
  }
  const entry = item.resume || item.progress;
  const season = entry?.season || 1;
  const episode = entry?.episode || 1;
  playTitle(item.type, item.tmdbId, item.title, season, episode, item);
}

async function playTitle(type, tmdbId, title, season = 1, episode = 1, item = null) {
  const detailsItem = item || state.detailsItem || {};
  if (state.kids && detailsItem.kidsSafe !== true) {
    showToast("Kids mode is on — this title is outside the kid-safe list.");
    return;
  }
  randomModal.classList.add("hidden");
  settingsModal.classList.add("hidden");
  clearInterval(state.settingsTimer);
  const playerItem = {
    ...libraryPayload(detailsItem),
    type,
    tmdbId,
    title: title || detailsItem.title || "",
    year: detailsItem.year || "",
  };
  const resume = detailsItem.resume || detailsItem.progress || null;
  state.player = {
    type,
    tmdbId,
    title,
    baseTitle: detailsItem.title || title,
    season,
    episode,
    item: playerItem,
    streams: [],
    captions: [],
    resume: resume && resume.position != null ? resume : null,
    sourcesDismissed: false,
  };
  state.streamIndex = -1;
  state.triedFallback = false;
  state.lastProgressAt = 0;
  stopTorrentPolling();
  playerModal.classList.remove("hidden");
  detailsModal.classList.add("hidden");
  document.body.style.overflow = "hidden";
  playerTitle.textContent = title || "Player";
  playerStatus.textContent = "Resolving stream…";
  playerStatus.classList.remove("hidden");
  quality.innerHTML = "";
  playerNote.textContent = "";
  castMenu.classList.add("hidden");
  setCastButton(false);
  refreshCastState();
  video.pause();
  video.removeAttribute("src");
  video.load();
  try {
    const params = new URLSearchParams({ tmdbId, type, season: String(season), episode: String(episode) });
    const data = await api(`/api/play?${params.toString()}`);
    state.player.streams = data.streams || [];
    state.player.captions = data.captions || [];
    state.player.resume = data.resume || null;
    renderCaptions(state.player.captions);
    if (!state.player.streams.length) throw new Error("No playable streams found. Search torrents instead?");
    renderQuality(data.streams);
    const capped = data.streams
      .map((stream, index) => ({ stream, index }))
      .filter((entry) => entry.stream.resolution && entry.stream.resolution <= 720)
      .pop();
    setStream(capped ? capped.index : data.streams.length - 1);
    if (data.resume && data.resume.position > 10) {
      showToast(`Resuming at ${formatClock(data.resume.position)}`);
    }
  } catch (error) {
    playerStatus.textContent = error.message || "Could not resolve this stream.";
    showToast(error.message || "Playback unavailable.");
    if (!state.kids && !state.player?.sourcesDismissed) {
      openSources(
        {
          type,
          tmdbId,
          title: detailsItem.title || title || "",
          displayTitle: title || detailsItem.title || "",
          year: detailsItem.year || "",
          season,
          episode,
          item: playerItem,
        },
        { auto: true },
      );
    }
  }
}

function formatClock(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function renderQuality(streams) {
  quality.innerHTML = streams
    .map((stream, index) => {
      const label = stream.label || (stream.resolution ? `${stream.resolution}p` : "Auto");
      const title = stream.kind === "download" ? "Downloaded file" : stream.torrent ? "WebTorrent source" : "Catalog source";
      return `<button type="button" data-index="${index}" title="${esc(title)}">${esc(label)}</button>`;
    })
    .join("");
  $$("button", quality).forEach((button) => {
    button.addEventListener("click", () => setStream(Number(button.dataset.index)));
  });
}

function renderCaptions(captions) {
  $$("track", video).forEach((track) => track.remove());
  (captions || []).forEach((caption) => {
    if (!caption || !caption.token) return;
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = caption.name || caption.lang || "Subtitle";
    track.srclang = (caption.lang || "en").slice(0, 8);
    track.src = `/api/subtitle?token=${encodeURIComponent(caption.token)}`;
    video.appendChild(track);
  });
}

function setStream(index) {
  const stream = state.player?.streams?.[index];
  if (!stream) return;
  state.streamIndex = index;
  state.triedFallback = false;
  $$("button", quality).forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.index) === index);
  });
  const directUrl = stream.url || "";
  playerStatus.textContent = directUrl
    ? "Opening from Downloads…"
    : stream.torrent
      ? "Connecting to torrent peers…"
      : "Buffering…";
  playerStatus.classList.remove("hidden");
  if (directUrl) {
    stopTorrentPolling();
    video.src = directUrl;
    playerNote.textContent = "Playing from Downloads. The file is stored locally on this server; progress is saved automatically.";
  } else if (stream.torrent) {
    stopTorrentPolling();
    state.torrentStartedAt = Date.now();
    video.src = `/api/torrents/stream?token=${encodeURIComponent(stream.token)}`;
    playerNote.textContent = "WebTorrent source: the server fetches pieces sequentially. The first seconds can take longer than a catalog stream.";
    startTorrentPolling(stream);
  } else {
    stopTorrentPolling();
    video.src = `/api/video?token=${encodeURIComponent(stream.token)}`;
    playerNote.textContent = "Stream is proxied through this server; signed source expires automatically. Progress is saved automatically.";
  }
  video.load();
  video.play().catch(() => {});
}

function tryLowerQuality() {
  if (!state.player || !state.player.streams) return false;
  const next = state.streamIndex - 1;
  if (next < 0) return false;
  setStream(next);
  return true;
}

function formatBytes(value) {
  const number = Number(value) || 0;
  if (number < 1024) return `${Math.round(number)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = number;
  let unit = -1;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
}

function stopTorrentPolling() {
  clearInterval(state.torrentTimer);
  state.torrentTimer = null;
  hideSlowPrompt(false);
}

function formatEta(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function hideSlowPrompt(dismiss = false) {
  const token = state.torrentPromptToken;
  if (dismiss && token) state.torrentDismissed[token] = Date.now();
  state.torrentPromptToken = "";
  slowPrompt.classList.add("hidden");
}

function showSlowPrompt(stream, status) {
  const peers = Number(status.numPeers || 0);
  const speed = Number(status.downloadSpeed || 0);
  const progress = Number(status.fileProgress ?? status.progress ?? 0);
  const eta = Number(status.timeRemaining || 0);
  const bits = [];
  bits.push(peers ? `${peers} peer${peers === 1 ? "" : "s"}` : "no peers right now");
  if (speed > 0) bits.push(`~${formatBytes(speed)}/s`);
  if (progress > 0) bits.push(`${Math.round(progress * 100)}% downloaded`);
  if (eta > 0 && Number.isFinite(eta) && eta < 24 * 3600) bits.push(`about ${formatEta(eta)} left`);
  state.torrentPromptToken = stream.token;
  slowPromptText.textContent = `This torrent is taking longer than expected (${bits.join(" · ")}). Would you like to add it to Downloads? We'll keep downloading it in the background so you can watch it later.`;
  slowPrompt.classList.remove("hidden");
}

function maybeOfferDownload(stream, status) {
  if (!stream?.torrent || !stream.token || stream.keep) return;
  if (state.torrentPromptToken === stream.token && !slowPrompt.classList.contains("hidden")) return;
  const now = Date.now();
  const dismissedAt = Number(state.torrentDismissed[stream.token] || 0);
  if (dismissedAt && now - dismissedAt < 10 * 60 * 1000) return;
  const elapsed = now - state.torrentStartedAt;
  if (elapsed < 20000) return;
  if (now - Number(state.torrentSeekingAt || 0) < 15000) return;
  const progress = Number(status.fileProgress ?? status.progress ?? 0);
  if (progress >= 0.98) return;
  const peers = Number(status.numPeers || 0);
  const speed = Number(status.downloadSpeed || 0);
  const eta = Number(status.timeRemaining || 0);
  const notPlayable = video.readyState < 2;
  const buffering = video.readyState < 3 && !video.paused;
  const slow = peers === 0 || (speed > 0 && speed < 250 * 1024) || (eta > 20 * 60 && Number.isFinite(eta));
  if (notPlayable && elapsed >= 25000) {
    showSlowPrompt(stream, status);
    return;
  }
  if (buffering && slow && elapsed >= 30000) showSlowPrompt(stream, status);
}

async function addCurrentTorrentToDownloads() {
  const stream = state.player?.streams?.[state.streamIndex];
  if (!stream?.torrent || !stream.token) {
    showToast("No torrent source is active.");
    return;
  }
  const player = state.player || {};
  const item = player.item || {};
  const original = slowDownloadButton.textContent;
  slowDownloadButton.disabled = true;
  slowDownloadButton.textContent = "Adding…";
  stream.keep = true;
  try {
    await api("/api/downloads/add", {
      method: "POST",
      body: JSON.stringify({
        token: stream.token,
        title: player.baseTitle || player.title || playerTitle.textContent || "Torrent",
        baseTitle: player.baseTitle || player.title || "",
        displayTitle: player.title || playerTitle.textContent || "",
        mediaType: player.type || "movie",
        tmdbId: player.tmdbId || "",
        year: item.year || "",
        poster: item.posterRaw || item.poster || "",
        backdrop: item.backdropRaw || item.backdrop || "",
        season: player.season || 1,
        episode: player.episode || 1,
        item: {
          type: player.type || "movie",
          tmdbId: player.tmdbId || "",
          title: player.baseTitle || player.title || "",
          year: item.year || "",
          poster: item.posterRaw || item.poster || "",
          backdrop: item.backdropRaw || item.backdrop || "",
        },
      }),
    });
    stream.keep = true;
    hideSlowPrompt(false);
    stopTorrentPolling();
    showToast("Added to Downloads — we'll keep downloading it in the background.");
    closePlayer();
    setView("downloads");
    loadDownloads();
  } catch (error) {
    stream.keep = false;
    showToast(error.message || "Could not add this torrent to Downloads.");
  } finally {
    slowDownloadButton.disabled = false;
    slowDownloadButton.textContent = original;
  }
}

function startTorrentPolling(stream) {
  stopTorrentPolling();
  const tick = async () => {
    const current = state.player?.streams?.[state.streamIndex];
    if (!current || current.token !== stream.token) {
      stopTorrentPolling();
      return;
    }
    try {
      const data = await api(`/api/torrents/status?token=${encodeURIComponent(stream.token)}`);
      const status = data.status || {};
      const parts = ["WebTorrent", `${status.numPeers || 0} peers`];
      if (status.downloadSpeed) parts.push(`${formatBytes(status.downloadSpeed)}/s`);
      const progress = Number(status.fileProgress ?? status.progress ?? 0);
      if (progress > 0) parts.push(`${Math.round(progress * 100)}% downloaded`);
      playerNote.textContent = parts.join(" · ");
      if (!status.numPeers && Date.now() - state.torrentStartedAt > 12000 && !video.readyState) {
        playerStatus.textContent = "No peers yet. The swarm may be empty — you can keep waiting or add it to Downloads.";
        playerStatus.classList.remove("hidden");
      } else if (status.numPeers && video.readyState < 2) {
        playerStatus.textContent = "Downloading pieces from peers…";
        playerStatus.classList.remove("hidden");
      }
      maybeOfferDownload(stream, status);
    } catch {
      stopTorrentPolling();
    }
  };
  state.torrentTimer = setInterval(tick, 4000);
  tick();
}

function setCastButton(active, name = "") {
  castBtn.classList.toggle("on", !!active);
  castBtn.textContent = active ? "Casting" : "Cast";
  castBtn.title = active ? `Casting to ${name || "TV"}` : "Cast to a TV";
}

function stopCastPolling() {
  clearInterval(state.castTimer);
  state.castTimer = null;
}

function startCastPolling() {
  clearInterval(state.castTimer);
  state.castTimer = setInterval(async () => {
    if (!state.player) {
      stopCastPolling();
      return;
    }
    try {
      const status = await api("/api/cast/status");
      if (status.active) setCastButton(true, status.device?.name || "TV");
      else {
        setCastButton(false);
        stopCastPolling();
      }
    } catch {
      /* the cast connection may be reconnecting */
    }
  }, 5000);
}

async function refreshCastState() {
  try {
    const status = await api("/api/cast/status");
    if (status.active) {
      setCastButton(true, status.device?.name || "TV");
      startCastPolling();
    } else {
      setCastButton(false);
    }
  } catch {
    setCastButton(false);
  }
}

async function openCastMenu() {
  if (!state.player || state.streamIndex < 0) {
    showToast("Start playback first.");
    return;
  }
  if (!castMenu.classList.contains("hidden")) {
    castMenu.classList.add("hidden");
    return;
  }
  castMenu.classList.remove("hidden");
  castMenu.innerHTML = `<p class="player-note">Looking for Cast devices…</p>`;
  try {
    const [data, status] = await Promise.all([
      api("/api/cast/devices"),
      api("/api/cast/status").catch(() => null),
    ]);
    renderCastMenu(data, status);
  } catch (error) {
    castMenu.innerHTML = `<p class="player-note">${esc(error.message)}</p>`;
  }
}

function renderCastMenu(data, status) {
  if (!data.available) {
    castMenu.innerHTML = `
      <h3>Cast</h3>
      <p class="player-note">${esc(data.error || "Casting is unavailable.")}</p>
      <p class="player-note">Then restart the server with <code>--host 0.0.0.0</code>.</p>`;
    return;
  }
  const casting = !!status?.active;
  const currentId = status?.device?.id || "";
  const rows = (data.devices || [])
    .map(
      (device) => `
        <button class="cast-device ${device.id === currentId ? "current" : ""}" type="button" data-cast-id="${esc(device.id)}">
          <span>${esc(device.name)}</span>
          <small>${esc(device.model || "Cast")}</small>
        </button>`
    )
    .join("");
  castMenu.innerHTML = `
    <h3>Cast to</h3>
    ${rows || `<p class="player-note">No Cast devices found on this network.</p>`}
    ${casting ? `<button class="cast-device" type="button" data-cast-stop="1"><span>Stop casting</span><small>${esc(status.device?.name || "")}</small></button>` : ""}
    <p class="player-note">The TV must be on the same network as this server.</p>`;
  $$("[data-cast-id]", castMenu).forEach((button) => {
    button.addEventListener("click", () => castToDevice(button.dataset.castId));
  });
  const stop = $("[data-cast-stop]", castMenu);
  if (stop) stop.addEventListener("click", stopCasting);
}

async function castToDevice(deviceId) {
  const stream = state.player?.streams?.[state.streamIndex];
  if (!stream) {
    showToast("Start playback first.");
    return;
  }
  castMenu.innerHTML = `<p class="player-note">Sending the stream to the TV…</p>`;
  try {
    const data = await api("/api/cast/play", {
      method: "POST",
      body: JSON.stringify({
        device_id: deviceId,
        token: stream.token,
        title: playerTitle.textContent || state.player.title || "",
        position: video.currentTime || 0,
        thumb: state.player.item?.poster || "",
      }),
    });
    video.pause();
    setCastButton(true, data.device?.name || "TV");
    playerNote.textContent = `Casting to ${data.device?.name || "TV"}. Use the TV remote to control playback.`;
    castMenu.classList.add("hidden");
    showToast(`Casting to ${data.device?.name || "TV"}.`);
    startCastPolling();
  } catch (error) {
    castMenu.innerHTML = `<p class="player-note">${esc(error.message)}</p>`;
    showToast(error.message);
  }
}

async function stopCasting() {
  try {
    await api("/api/cast/stop", { method: "POST" });
  } catch {
    /* the device may already be idle */
  }
  setCastButton(false);
  stopCastPolling();
  castMenu.classList.add("hidden");
  if (state.player) playerNote.textContent = "Cast stopped. Local playback is ready.";
  showToast("Cast stopped.");
}

function sendProgress(force = false) {
  if (!state.player || !state.player.item || !state.player.item.tmdbId || !video.duration) return;
  const now = Date.now();
  if (!force && now - state.lastProgressAt < 8000) return;
  state.lastProgressAt = now;
  const player = state.player;
  api("/api/library", {
    method: "POST",
    body: JSON.stringify({
      action: "progress",
      item: player.item,
      position: video.currentTime,
      duration: video.duration,
      season: player.season,
      episode: player.episode,
    }),
  }).catch(() => {});
}

video.addEventListener("loadedmetadata", () => {
  const resume = state.player?.resume;
  if (resume && resume.position > 10 && video.duration && resume.position < video.duration - 15) {
    try {
      video.currentTime = Number(resume.position);
    } catch {
      /* ignore */
    }
  }
});
video.addEventListener("playing", () => playerStatus.classList.add("hidden"));
video.addEventListener("waiting", () => {
  playerStatus.textContent = "Buffering…";
  playerStatus.classList.remove("hidden");
});
video.addEventListener("seeking", () => {
  state.torrentSeekingAt = Date.now();
});
video.addEventListener("canplay", () => playerStatus.classList.add("hidden"));
video.addEventListener("timeupdate", () => sendProgress(false));
video.addEventListener("pause", () => {
  if (!playerModal.classList.contains("hidden")) sendProgress(true);
});
video.addEventListener("ended", async () => {
  if (state.player) {
    try {
      await api("/api/library", {
        method: "POST",
        body: JSON.stringify({
          action: "progress",
          item: state.player.item,
          position: video.duration || 0,
          duration: video.duration || 0,
          season: state.player.season,
          episode: state.player.episode,
        }),
      });
    } catch {
      /* ignore */
    }
    loadLibrary(true);
  }
  playerStatus.textContent = "Finished. Pick another title?";
  playerStatus.classList.remove("hidden");
});
video.addEventListener("error", () => {
  if (!state.triedFallback && tryLowerQuality()) {
    state.triedFallback = true;
    return;
  }
  const stream = state.player?.streams?.[state.streamIndex];
  playerStatus.textContent = stream?.torrent
    ? "This torrent source failed. Try an MP4/WebM release or another quality."
    : "This source is unavailable right now. Try another title or quality.";
  playerStatus.classList.remove("hidden");
});

function releaseTorrent(stream) {
  if (!stream?.torrent || !stream.token || stream.keep) return;
  api("/api/torrents/remove", { method: "POST", body: JSON.stringify({ token: stream.token }) }).catch(() => {});
}

function closePlayer() {
  sendProgress(true);
  const streams = state.player?.streams || [];
  stopTorrentPolling();
  streams.forEach((entry) => releaseTorrent(entry));
  video.pause();
  video.removeAttribute("src");
  $$("track", video).forEach((track) => track.remove());
  video.load();
  playerModal.classList.add("hidden");
  sourcesModal.classList.add("hidden");
  sourcesSearchForm.classList.add("hidden");
  castMenu.classList.add("hidden");
  stopCastPolling();
  setCastButton(false);
  document.body.style.overflow = detailsModal.classList.contains("hidden") ? "" : "hidden";
  playerStatus.classList.add("hidden");
  quality.innerHTML = "";
  state.player = null;
  state.sourcesContext = null;
  state.sourcesData = null;
  state.sourcesReturnPlayer = false;
}

// -- torrent sources --------------------------------------------------------

function sourcesOverflow() {
  const open = !playerModal.classList.contains("hidden")
    || !detailsModal.classList.contains("hidden")
    || !randomModal.classList.contains("hidden")
    || !settingsModal.classList.contains("hidden")
    || !sourcesModal.classList.contains("hidden");
  document.body.style.overflow = open ? "hidden" : "";
}

function abortSourcesRequest() {
  const controller = state.sourcesAbort;
  state.sourcesAbort = null;
  state.sourcesLoading = false;
  if (!controller) return;
  try {
    controller.abort();
  } catch {
    /* already aborted */
  }
}

function closeSources() {
  abortSourcesRequest();
  sourcesModal.classList.add("hidden");
  state.sourcesLoading = false;
  const restore = state.sourcesReturnPlayer && state.player;
  state.sourcesReturnPlayer = false;
  if (restore) {
    if (state.player) state.player.sourcesDismissed = true;
    playerModal.classList.remove("hidden");
    detailsModal.classList.add("hidden");
  }
  sourcesOverflow();
}

function sourcesContextLabel(context) {
  const parts = [context.type === "tv" ? "Series" : "Movie"];
  if (context.type === "tv") parts.push(`S${context.season || 1} E${context.episode || 1}`);
  if (context.year) parts.push(String(context.year));
  return parts.join(" · ");
}

function openSources(context, options = {}) {
  if (state.kids) {
    showToast("Turn off Kids mode to search torrents.");
    return;
  }
  abortSourcesRequest();
  const normalized = {
    type: context?.type === "tv" ? "tv" : "movie",
    tmdbId: context?.tmdbId || "",
    title: context?.title || "",
    displayTitle: context?.displayTitle || context?.title || "",
    year: context?.year || "",
    season: Number(context?.season || 1),
    episode: Number(context?.episode || 1),
    item: context?.item || null,
    global: !!context?.global,
    query: context?.query || "",
  };
  state.sourcesContext = normalized;
  state.sourcesData = null;
  state.sourcesMode = normalized.global || options.search ? "search" : "title";
  state.sourcesReturnPlayer = !playerModal.classList.contains("hidden");
  if (state.sourcesReturnPlayer) video.pause();
  sourcesModal.classList.remove("hidden");
  playerModal.classList.add("hidden");
  detailsModal.classList.add("hidden");
  document.body.style.overflow = "hidden";
  sourcesQuality.value = "";
  sourcesSort.value = "seeds";
  sourcesWebOnly.checked = false;
  sourcesProviders.innerHTML = "";
  sourcesList.innerHTML = "";
  sourcesNote.textContent = "";
  sourcesSearchForm.classList.toggle("hidden", state.sourcesMode !== "search");
  sourcesType.value = normalized.type;
  sourcesSeason.value = String(normalized.season || 1);
  sourcesEpisode.value = String(normalized.episode || 1);
  sourcesType.dispatchEvent(new Event("change"));
  if (state.sourcesMode === "search") {
    sourcesTitle.textContent = normalized.query || normalized.title || "Search torrents";
    sourcesSub.textContent = "Find missing content across Torrentio, The Pirate Bay, YTS and EZTV.";
    sourcesQuery.value = normalized.query || normalized.title || "";
    setTimeout(() => sourcesQuery.focus(), 30);
    if (options.autoSearch !== false && sourcesQuery.value.trim()) loadTorrentSources(true);
  } else {
    sourcesTitle.textContent = normalized.displayTitle || normalized.title || "Choose a source";
    sourcesSub.textContent = `${sourcesContextLabel(normalized)} · searching public torrent indexes`;
    loadTorrentSources(true);
  }
}

async function loadTorrentSources(refresh = false) {
  abortSourcesRequest();
  const context = state.sourcesContext;
  if (!context) return;
  const params = new URLSearchParams();
  if (state.sourcesMode === "search") {
    const query = sourcesQuery.value.trim();
    if (!query && !sourcesType.value) {
      showToast("Type a title to search.");
      return;
    }
    params.set("q", query);
    params.set("type", sourcesType.value);
    params.set("season", sourcesSeason.value || "1");
    params.set("episode", sourcesEpisode.value || "1");
  } else {
    params.set("type", context.type);
    params.set("season", String(context.season || 1));
    params.set("episode", String(context.episode || 1));
    if (context.tmdbId) params.set("tmdbId", context.tmdbId);
    if (context.title) params.set("title", context.title);
    if (context.year) params.set("year", context.year);
  }
  if (refresh) params.set("refresh", "1");
  if (state.sourcesMode === "search") {
    sourcesTitle.textContent = sourcesQuery.value.trim() || "Search torrents";
  }
  abortSourcesRequest();
  const controller = new AbortController();
  state.sourcesAbort = controller;
  state.sourcesLoading = true;
  sourcesList.innerHTML = `<div class="source-loading"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
  sourcesNote.textContent = "Searching Torrentio, The Pirate Bay, YTS and EZTV…";
  sourcesRefresh.disabled = true;
  try {
    const data = await api(`/api/torrents/search?${params.toString()}`, { signal: controller.signal });
    state.sourcesData = data;
    const providerText = (data.providers || []).map((provider) => `${provider.name}: ${provider.ok ? provider.count : "error"}`).join(" · ");
    sourcesSub.textContent = `${sourcesContextLabel(context)} · ${data.count} source${data.count === 1 ? "" : "s"}${data.imdbId ? ` · ${data.imdbId}` : ""}`;
    sourcesProviders.innerHTML = (data.providers || []).map((provider) => `
      <span class="provider-pill ${provider.ok ? "" : "off"}" title="${esc(provider.error || "")}">${esc(provider.name)}${provider.ok ? ` · ${provider.count}` : ""}</span>
    `).join("");
    populateSourceQualities(data.results || []);
    renderSourceRows();
    const bridge = data.bridge || {};
    const lines = [providerText || "No provider returned data."];
    if (!data.imdbId) {
      lines.push("No IMDb match was found, so IMDb-based providers were skipped.");
    }
    if (bridge.installed === false) {
      lines.push("The WebTorrent bridge is not installed yet. Run `cd torrent_bridge && npm install` (or `npm.cmd install` in PowerShell) to enable torrent playback.");
    } else if (!bridge.node) {
      lines.push("Node.js was not found. Install Node.js 20 or newer to enable torrent playback.");
    } else if (data.cached) {
      lines.push("Cached search results. Use Refresh for a fresh sweep.");
    }
    sourcesNote.textContent = lines.join(" ");
  } catch (error) {
    if (error.name === "AbortError") return;
    sourcesList.innerHTML = `
      <div class="source-empty">
        <p>${esc(error.message || "Torrent search failed.")}</p>
        <p class="hint">${esc(error.data?.bridge?.installHint || "")}</p>
      </div>
    `;
    sourcesNote.textContent = error.data?.bridge?.installHint
      ? `Optional bridge setup: ${error.data.bridge.installHint}`
      : "Try again, or search a different title.";
    sourcesSub.textContent = "Torrent search failed";
    sourcesProviders.innerHTML = "";
  } finally {
    if (state.sourcesAbort === controller) state.sourcesAbort = null;
    state.sourcesLoading = false;
    sourcesRefresh.disabled = false;
  }
}

function populateSourceQualities(results) {
  const values = [...new Set(results.map((result) => Number(result.qualityValue) || 0))]
    .filter(Boolean)
    .sort((a, b) => b - a);
  const current = sourcesQuality.value;
  sourcesQuality.innerHTML = `<option value="">All</option>${values.map((value) => `<option value="${value}">${value}p</option>`).join("")}`;
  sourcesQuality.value = values.includes(Number(current)) ? current : "";
}

function sourceExtension(result) {
  const name = result.filename || result.title || "";
  const match = /\.([a-z0-9]{2,4})$/i.exec(name);
  return match ? match[1].toUpperCase() : "";
}

function renderSourceRows() {
  const data = state.sourcesData;
  if (!data) return;
  const quality = Number(sourcesQuality.value) || 0;
  const webOnly = sourcesWebOnly.checked;
  let results = (data.results || []).filter((result) => {
    if (quality && Number(result.qualityValue) !== quality) return false;
    if (webOnly && !result.webFriendly) return false;
    return true;
  });
  const sort = sourcesSort.value;
  results = results.slice().sort((a, b) => {
    if (sort === "size") return (b.sizeBytes || 0) - (a.sizeBytes || 0);
    if (sort === "quality") return (b.qualityValue || 0) - (a.qualityValue || 0) || (b.seeders || 0) - (a.seeders || 0);
    return (b.seeders || 0) - (a.seeders || 0) || (b.qualityValue || 0) - (a.qualityValue || 0);
  });
  state.sourcesView = results;
  const firstWeb = results.findIndex((result) => result.webFriendly);
  if (!results.length) {
    sourcesList.innerHTML = data.count
      ? `<div class="source-empty"><p>No sources match this filter.</p><p class="hint">Clear the quality filter or uncheck MP4/WebM only.</p></div>`
      : `<div class="source-empty"><p>No torrent sources found.</p><p class="hint">Try the original title, add a year, or check the spelling.</p></div>`;
    return;
  }
  sourcesList.innerHTML = results.slice(0, 160).map((result, index) => {
    const ext = sourceExtension(result);
    const providers = (result.providers && result.providers.length ? result.providers : [result.provider]).filter(Boolean).join(" · ");
    const qualityLabel = result.quality || "Unknown";
    return `
      <article class="source-row" data-source-index="${index}"${index === firstWeb ? ' id="source-first-web"' : ""}>
        <div class="source-main">
          <p class="source-name" title="${esc(result.title || result.filename || "")}">${esc(result.filename || result.title || "Torrent")}</p>
          <div class="source-meta">
            <span class="source-provider">${esc(providers)}</span>
            <span class="source-q">${esc(qualityLabel)}</span>
            <span>${esc(formatBytes(result.sizeBytes))}</span>
            <span class="source-seeds">▲ ${esc(result.seeders || 0)}</span>
            ${ext ? `<span class="source-ext ${result.webFriendly ? "good" : "warn"}" title="${result.webFriendly ? "Browser-friendly container" : "The browser may not play this container or codec"}">${esc(ext)}</span>` : ""}
          </div>
        </div>
        <button class="btn primary source-play" type="button">Play</button>
      </article>
    `;
  }).join("");
  $$(".source-row", sourcesList).forEach((row) => {
    const index = Number(row.dataset.sourceIndex);
    const button = $(".source-play", row);
    button.addEventListener("click", () => resolveTorrentSource(results[index], button));
  });
}

async function resolveTorrentSource(result, button) {
  if (!result) return;
  abortSourcesRequest();
  const controller = new AbortController();
  state.sourcesAbort = controller;
  state.sourcesLoading = true;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Connecting…";
  sourcesNote.textContent = `Connecting to ${result.provider || "torrent"} · ${result.seeders || 0} seeders. Waiting for metadata (up to 30s)…`;
  try {
    const data = await api("/api/torrents/play", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        magnet: result.magnet,
        fileIndex: result.fileIndex,
        fileHint: result.fileHint,
        title: state.sourcesContext?.title || result.title,
        qualityValue: result.qualityValue,
        sizeBytes: result.sizeBytes,
      }),
    });
    if (data.needsFile) {
      renderTorrentFiles(data, result);
      return;
    }
    playTorrentStream(data, result);
  } catch (error) {
    if (error.name === "AbortError") return;
    sourcesNote.textContent = error.message || "Could not start this torrent.";
    showToast(error.message || "Torrent is unavailable.");
    button.disabled = false;
    button.textContent = original;
  } finally {
    if (state.sourcesAbort === controller) state.sourcesAbort = null;
    state.sourcesLoading = false;
  }
}

function renderTorrentFiles(data, result) {
  const files = data.files || [];
  sourcesProviders.innerHTML = `<span class="provider-pill">${esc(result.provider || "Torrent")}</span><span class="provider-pill">${files.length} files</span>`;
  sourcesNote.textContent = "This torrent contains more than one video. Choose the file to play.";
  sourcesList.innerHTML = `
    <div class="source-empty source-files-head">
      <p>${esc(data.name || result.title || "Torrent")}</p>
      <p class="hint">Pick the exact episode or file. The largest file is preselected.</p>
    </div>
    ${files.map((file) => `
      <button class="source-file" type="button" data-file-index="${file.index}">
        <span class="source-file-name">${esc(file.name || `File ${file.index}`)}</span>
        <span class="source-file-size">${esc(formatBytes(file.length))}</span>
        <span class="episode-play">▶</span>
      </button>
    `).join("")}
  `;
  $$("[data-file-index]", sourcesList).forEach((button) => {
    button.addEventListener("click", async () => {
      abortSourcesRequest();
      const controller = new AbortController();
      state.sourcesAbort = controller;
      state.sourcesLoading = true;
      button.disabled = true;
      sourcesNote.textContent = "Opening the selected file…";
      try {
        const data2 = await api("/api/torrents/play", {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            key: data.key,
            fileIndex: Number(button.dataset.fileIndex),
            qualityValue: result?.qualityValue,
            sizeBytes: result?.sizeBytes,
          }),
        });
        playTorrentStream(data2, result);
      } catch (error) {
        if (error.name === "AbortError") return;
        sourcesNote.textContent = error.message;
        showToast(error.message);
        button.disabled = false;
      } finally {
        if (state.sourcesAbort === controller) state.sourcesAbort = null;
        state.sourcesLoading = false;
      }
    });
  });
}

function openTorrentPlayer(context) {
  const item = context.item || {
    type: context.type,
    title: context.title || "Torrent",
    year: context.year || "",
  };
  state.player = {
    type: context.type || "movie",
    tmdbId: context.tmdbId || "",
    title: context.displayTitle || context.title || "Torrent",
    baseTitle: context.title || "Torrent",
    season: Number(context.season || 1),
    episode: Number(context.episode || 1),
    item,
    streams: [],
    captions: [],
    resume: item.resume || item.progress || null,
    sourcesDismissed: false,
  };
  state.streamIndex = -1;
  state.triedFallback = false;
  state.lastProgressAt = 0;
  playerModal.classList.remove("hidden");
  detailsModal.classList.add("hidden");
  document.body.style.overflow = "hidden";
  playerTitle.textContent = context.displayTitle || context.title || "Torrent";
  playerStatus.textContent = "Resolving torrent…";
  playerStatus.classList.remove("hidden");
  quality.innerHTML = "";
  playerNote.textContent = "";
  castMenu.classList.add("hidden");
  setCastButton(false);
  refreshCastState();
  video.pause();
  video.removeAttribute("src");
  video.load();
}

function playTorrentStream(data, result) {
  const context = state.sourcesContext || { type: "movie", title: result?.title || "Torrent", season: 1, episode: 1 };
  const sameContext = state.player
    && String(state.player.type) === String(context.type)
    && String(state.player.tmdbId || "") === String(context.tmdbId || "")
    && String(state.player.baseTitle || state.player.title || "") === String(context.title || "");
  if (!sameContext) openTorrentPlayer(context);
  if (sameContext && video.currentTime > 10) {
    state.player.resume = { position: video.currentTime, duration: video.duration || 0, season: state.player.season, episode: state.player.episode };
  }
  playerModal.classList.remove("hidden");
  detailsModal.classList.add("hidden");
  document.body.style.overflow = "hidden";
  const qualityLabel = data.stream?.label || result?.quality || "Auto";
  const stream = {
    kind: "torrent",
    torrent: true,
    token: data.token,
    resolution: Number(data.stream?.resolution || result?.qualityValue || 0),
    size: Number(data.stream?.size || result?.sizeBytes || 0),
    content_type: data.stream?.content_type || "video/mp4",
    label: qualityLabel && qualityLabel !== "Auto" ? `${qualityLabel} T` : "Torrent",
    infoHash: data.infoHash,
    fileName: data.fileName || result?.filename || "",
  };
  const streams = state.player.streams || [];
  const existing = streams.findIndex((entry) => entry.torrent && entry.token === stream.token);
  if (existing >= 0) streams[existing] = stream;
  else streams.push(stream);
  state.player.streams = streams;
  renderQuality(streams);
  setStream(existing >= 0 ? existing : streams.length - 1);
  closeSources();
}

// -- random -----------------------------------------------------------------

async function randomPick() {
  randomModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  $("#random-body").innerHTML = `<p class="player-note">Choosing something…</p>`;
  try {
    const data = await api(`/api/random?${filterParams().toString()}`);
    const item = data.item;
    cacheItems(data.options || []);
    syncCardCacheFlags();
    $("#random-body").innerHTML = `<div class="random-pick">${cardMarkup(item, { showReason: true })}</div>`;
    bindCards($("#random-body"));
  } catch (error) {
    $("#random-body").innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

// -- settings ---------------------------------------------------------------

async function renderSettings() {
  const body = $("#settings-body");
  body.innerHTML = `<p class="player-note">Loading metadata status…</p>`;
  try {
    const data = await api("/api/metadata");
    const coverage = data.coverage || {};
    const stats = data.stats || {};
    const torrentInfo = data.torrents || {};
    const downloadStats = data.downloads || {};
    body.innerHTML = `
      <section class="settings-block">
        <h3>Metadata coverage</h3>
        <div class="meter"><span style="width:${Math.min(100, Number(coverage.percent) || 0)}%"></span></div>
        <p>${coverage.rated ?? 0} of ${coverage.total ?? 0} titles have an age rating (${coverage.percent ?? 0}%). ${coverage.known ?? 0} have genre data.</p>
        <p class="hint">Genres and age ratings come from the upstream title endpoint and are cached locally. Kids mode only includes titles with a verified kid-safe rating.</p>
        <div class="settings-actions">
          <button class="btn primary" type="button" data-meta-action="missing">Enrich 500 missing</button>
          <button class="btn" type="button" data-meta-action="all">Enrich all missing</button>
        </div>
        <p class="player-note">Queue: ${data.queue ?? 0} · done: ${stats.done ?? 0} · failed: ${stats.failed ?? 0}</p>
      </section>
      <section class="settings-block">
        <h3>Internet lists</h3>
        <p>Live charts come from the catalog feed. Set a <code>TMDB_API_KEY</code> environment variable to add TMDB charts. Import any public JSON, CSV, text or HTML list from the Lists page.</p>
        <button class="btn" type="button" id="settings-refresh-lists">Refresh live lists</button>
      </section>
      <section class="settings-block">
        <h3>Torrent fallback (WebTorrent)</h3>
        <p>When a catalog stream is missing, the Sources panel searches Torrentio, The Pirate Bay, YTS and EZTV. You choose the release; the local Node bridge downloads it and streams it through this server.</p>
        <p class="hint">Bridge: ${esc(torrentInfo.running ? `running · ${torrentInfo.torrents ?? 0} active torrent(s)` : torrentInfo.installed ? "installed · starts on first torrent" : "not installed")}${torrentInfo.error ? ` · ${esc(torrentInfo.error)}` : ""}</p>
        <p class="hint">Background downloads: ${downloadStats.downloading ?? 0} downloading · ${downloadStats.done ?? 0} ready to watch. Completed files stay in <code>.app_cache/torrents</code> for local playback.</p>
        ${torrentInfo.installed ? "" : `<p class="hint">Install the bridge: <code>cd torrent_bridge &amp;&amp; npm install</code> (PowerShell: <code>npm.cmd install</code>). Node.js 20 or newer is required.</p>`}
        <p class="hint">Provider hosts can be overridden with TORRENTIO_BASE, APIBAY_BASE, YTS_BASE and EZTV_BASE. A TMDB_API_KEY improves IMDb matching for Stremio-style providers.</p>
      </section>
      <section class="settings-block">
        <h3>Age and kids mode</h3>
        <p>Kids mode filters the entire app to titles rated ${'U/G/TV-Y/TV-Y7/TV-G'} with a kid-friendly genre. The age filter is a maximum age, so “Up to 12” includes kids titles.</p>
        <label class="settings-toggle"><input id="settings-kids" type="checkbox" ${state.kids ? "checked" : ""}> Kids mode</label>
        <label class="settings-toggle">Default max age
          <select id="settings-max-age">
            <option value="" ${!state.maxAge ? "selected" : ""}>Any</option>
            <option value="7" ${state.maxAge === "7" ? "selected" : ""}>7+</option>
            <option value="12" ${state.maxAge === "12" ? "selected" : ""}>12+</option>
            <option value="15" ${state.maxAge === "15" ? "selected" : ""}>15+</option>
            <option value="18" ${state.maxAge === "18" ? "selected" : ""}>18+</option>
          </select>
        </label>
      </section>
      <section class="settings-block">
        <h3>Keyboard shortcuts</h3>
        <div class="shortcut-list">
          <span><kbd>/</kbd> search</span><span><kbd>R</kbd> random</span><span><kbd>K</kbd> kids mode</span>
          <span><kbd>F</kbd> For You</span><span><kbd>B</kbd> Browse</span><span><kbd>D</kbd> Downloads</span><span><kbd>L</kbd> My List</span><span><kbd>Esc</kbd> close</span>
        </div>
      </section>
      <section class="settings-block">
        <h3>Data</h3>
        <p class="hint">Library data is stored server-side in <code>state/userdata.json</code>. Metadata cache: <code>.app_cache/metadata.sqlite3</code>.</p>
      </section>
    `;
    $$("[data-meta-action]", body).forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        const mode = button.dataset.metaAction === "all" ? "all" : "missing";
        try {
          await api("/api/metadata", { method: "POST", body: JSON.stringify({ mode, limit: mode === "missing" ? 500 : 0 }) });
          showToast("Metadata enrichment started.");
          await renderSettings();
        } catch (error) {
          showToast(error.message);
        } finally {
          button.disabled = false;
        }
      });
    });
    $("#settings-refresh-lists")?.addEventListener("click", async () => {
      try {
        const result = await api("/api/lists/refresh", { method: "POST", body: JSON.stringify({}) });
        showToast(result.ok ? "Live lists refreshed." : result.error || "Could not refresh lists.");
      } catch (error) {
        showToast(error.message);
      }
    });
    $("#settings-kids")?.addEventListener("change", (event) => setKids(event.target.checked));
    $("#settings-max-age")?.addEventListener("change", async (event) => {
      state.maxAge = event.target.value;
      try {
        await api("/api/library", { method: "POST", body: JSON.stringify({ action: "settings", settings: { maxAge: state.maxAge || null } }) });
      } catch (error) {
        showToast(error.message);
      }
      showToast(state.maxAge ? `Showing titles rated up to ${state.maxAge}+` : "Age cap cleared.");
      if (state.view === "browse") loadBrowse(true);
      else if (state.view === "for-you") loadForYou();
    });
  } catch (error) {
    body.innerHTML = `<p class="empty">${esc(error.message)}</p>`;
  }
}

function openSettings() {
  settingsModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  renderSettings();
  clearInterval(state.settingsTimer);
  state.settingsTimer = setInterval(() => {
    if (!settingsModal.classList.contains("hidden")) renderSettings();
  }, 8000);
}

function closeSettings() {
  settingsModal.classList.add("hidden");
  clearInterval(state.settingsTimer);
  state.settingsTimer = null;
  if (playerModal.classList.contains("hidden") && detailsModal.classList.contains("hidden") && randomModal.classList.contains("hidden")) {
    document.body.style.overflow = "";
  }
}

// -- kids mode --------------------------------------------------------------

function setKids(value) {
  state.kids = !!value;
  localStorage.setItem("pm.kids", state.kids ? "1" : "0");
  document.body.classList.toggle("kids-mode", state.kids);
  const toggle = $("#kids-toggle");
  if (toggle) toggle.checked = state.kids;
  if (state.kids) {
    state.genre = "";
    state.age = "";
    state.maxAge = "";
    state.minRating = "";
    state.maxRuntime = "";
    state.decade = "";
    state.sort = "popular";
  }
  showToast(state.kids ? "Kids mode on — only kid-safe titles are shown." : "Kids mode off.");
  if (state.view === "for-you") loadForYou();
  else if (state.view === "browse") loadBrowse(true);
  else if (state.view === "lists") loadLists();
  else if (state.view === "library") loadLibraryView();
  else if (state.view === "list" && state.list) openList(state.list.id, false);
}

// -- global events ----------------------------------------------------------

$$(".nav-btn").forEach((button) => {
  button.addEventListener("click", () => {
    setView(button.dataset.view);
    if (button.dataset.view === "for-you") {
      clearSearch();
      loadForYou();
    } else if (button.dataset.view === "browse") loadBrowse(true);
    else if (button.dataset.view === "lists") loadLists();
    else if (button.dataset.view === "downloads") loadDownloads();
    else if (button.dataset.view === "library") loadLibraryView();
  });
});

$$("[data-close='details']").forEach((element) => element.addEventListener("click", closeDetails));
$$("[data-close='player']").forEach((element) => element.addEventListener("click", closePlayer));
$$("[data-close='random']").forEach((element) => {
  element.addEventListener("click", () => {
    randomModal.classList.add("hidden");
    document.body.style.overflow = "";
  });
});
$$("[data-close='settings']").forEach((element) => element.addEventListener("click", closeSettings));
castBtn.addEventListener("click", openCastMenu);
sourcesBtn.addEventListener("click", () => {
  const player = state.player;
  if (!player) {
    openSources({ global: true, query: "" }, { search: true, autoSearch: false });
    return;
  }
  openSources({
    type: player.type,
    tmdbId: player.tmdbId,
    title: player.baseTitle || player.title,
    displayTitle: player.title,
    year: player.item?.year || "",
    season: player.season,
    episode: player.episode,
    item: player.item,
  });
});
sourcesSearchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadTorrentSources(true);
});
sourcesType.addEventListener("change", () => {
  const isTv = sourcesType.value === "tv";
  sourcesSeason.classList.toggle("hidden", !isTv);
  sourcesEpisode.classList.toggle("hidden", !isTv);
});
sourcesQuality.addEventListener("change", renderSourceRows);
sourcesSort.addEventListener("change", renderSourceRows);
sourcesWebOnly.addEventListener("change", renderSourceRows);
sourcesRefresh.addEventListener("click", () => loadTorrentSources(true));
slowDownloadButton.addEventListener("click", addCurrentTorrentToDownloads);
slowDismissButton.addEventListener("click", () => {
  hideSlowPrompt(true);
  showToast("We'll keep trying. The Downloads offer will return if it stays slow.");
});
downloadsRefresh.addEventListener("click", () => loadDownloads());
$$("[data-close='sources']").forEach((element) => element.addEventListener("click", closeSources));
document.addEventListener("click", (event) => {
  if (castMenu.classList.contains("hidden")) return;
  if (castMenu.contains(event.target) || castBtn.contains(event.target)) return;
  castMenu.classList.add("hidden");
});

document.addEventListener("keydown", (event) => {
  const tag = (event.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || event.target.isContentEditable;
  if (event.key === "Escape") {
    if (!castMenu.classList.contains("hidden")) {
      castMenu.classList.add("hidden");
      return;
    }
    if (!sourcesModal.classList.contains("hidden")) {
      closeSources();
      return;
    }
    if (!playerModal.classList.contains("hidden")) closePlayer();
    else if (!detailsModal.classList.contains("hidden")) closeDetails();
    else if (!randomModal.classList.contains("hidden")) {
      randomModal.classList.add("hidden");
      document.body.style.overflow = "";
    } else if (!settingsModal.classList.contains("hidden")) closeSettings();
    return;
  }
  if (typing) return;
  if (event.key === "/") {
    event.preventDefault();
    searchInput.focus();
    searchInput.select();
  } else if (event.key.toLowerCase() === "r") randomPick();
  else if (event.key.toLowerCase() === "k") setKids(!state.kids);
  else if (event.key.toLowerCase() === "f") {
    setView("for-you");
    clearSearch();
    loadForYou();
  } else if (event.key.toLowerCase() === "b") {
    setView("browse");
    loadBrowse(true);
  } else if (event.key.toLowerCase() === "d") {
    setView("downloads");
    loadDownloads();
  } else if (event.key.toLowerCase() === "l") {
    setView("library");
    loadLibraryView();
  }
});

let searchTimer = null;
searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.q = searchInput.value.trim();
  setView("browse");
  loadBrowse(true);
});
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const query = searchInput.value.trim();
    if (query === state.q) return;
    state.q = query;
    if (state.view === "browse") loadBrowse(true);
  }, 420);
});

$("#kids-toggle").addEventListener("change", (event) => setKids(event.target.checked));
$("#random-btn").addEventListener("click", randomPick);
$("#settings-btn").addEventListener("click", openSettings);
$("#random-again").addEventListener("click", randomPick);
$("#random-browse").addEventListener("click", () => {
  randomModal.classList.add("hidden");
  document.body.style.overflow = "";
  setView("browse");
  loadBrowse(true);
});
$("#load-more").addEventListener("click", () => {
  if (state.page < state.pages && !state.loading) {
    state.page += 1;
    loadBrowse(false);
  }
});
$("#refresh-lists").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const result = await api("/api/lists/refresh", { method: "POST", body: JSON.stringify({}) });
    showToast(result.ok ? "Live lists refreshed." : result.error || "Could not refresh lists.");
    await loadLists();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh live lists";
  }
});
$("#import-url-btn").addEventListener("click", () => importList("url"));
$("#import-text-btn").addEventListener("click", () => importList("text"));
$("#list-back").addEventListener("click", () => {
  setView("lists");
  loadLists();
});
$("#delete-list").addEventListener("click", async () => {
  if (!state.list) return;
  if (!confirm(`Delete “${state.list.name}”?`)) return;
  try {
    await api(`/api/lists/${encodeURIComponent(state.list.id)}`, { method: "DELETE" });
    showToast("List deleted.");
    setView("lists");
    loadLists();
  } catch (error) {
    showToast(error.message);
  }
});

window.addEventListener("hashchange", route);

// -- boot -------------------------------------------------------------------

document.body.classList.toggle("kids-mode", state.kids);
$("#kids-toggle").checked = state.kids;
loadLibrary(true).finally(() => {
  route();
});
