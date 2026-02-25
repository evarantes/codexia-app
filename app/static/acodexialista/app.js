const STORAGE_KEY = "acodexialista:lista:v1";
const HISTORY_KEY = "acodexialista:historico:v1";
const PRIORITY_WEIGHT = { alta: 3, media: 2, baixa: 1 };

const BASE_SUGGESTIONS = [
  { name: "Arroz", category: "Mercearia", unit: "kg", quantity: 5, price: 34.9 },
  { name: "Feijao", category: "Mercearia", unit: "kg", quantity: 2, price: 19.9 },
  { name: "Acucar", category: "Mercearia", unit: "kg", quantity: 2, price: 11.5 },
  { name: "Cafe", category: "Mercearia", unit: "pct", quantity: 2, price: 16.9 },
  { name: "Ovo", category: "Mercearia", unit: "cx", quantity: 1, price: 17.0 },
  { name: "Leite", category: "Bebidas", unit: "l", quantity: 6, price: 6.2 },
  { name: "Banana", category: "Hortifruti", unit: "kg", quantity: 1, price: 7.5 },
  { name: "Tomate", category: "Hortifruti", unit: "kg", quantity: 1, price: 9.5 },
  { name: "Alface", category: "Hortifruti", unit: "un", quantity: 1, price: 4.2 },
  { name: "Sabao em po", category: "Limpeza", unit: "pct", quantity: 1, price: 22.9 },
  { name: "Detergente", category: "Limpeza", unit: "un", quantity: 3, price: 2.7 },
  { name: "Papel higienico", category: "Higiene", unit: "pct", quantity: 1, price: 21.9 },
  { name: "Shampoo", category: "Higiene", unit: "un", quantity: 1, price: 14.9 },
  { name: "Pao de forma", category: "Padaria", unit: "pct", quantity: 1, price: 8.9 },
  { name: "Frango congelado", category: "Congelados", unit: "kg", quantity: 1, price: 19.5 },
];

// Mapa simples de palavras-chave para sugestao automatica de categoria.
const CATEGORY_KEYWORDS = {
  Hortifruti: [
    "banana",
    "maca",
    "alface",
    "tomate",
    "cebola",
    "alho",
    "batata",
    "cenoura",
    "uva",
    "laranja",
    "mamao",
    "morango",
  ],
  Limpeza: ["detergente", "desinfetante", "agua sanitaria", "sabao", "amaciante", "esponja", "limpeza"],
  Higiene: ["sabonete", "shampoo", "condicionador", "pasta", "escova", "papel higienico", "higiene"],
  Mercearia: ["arroz", "feijao", "acucar", "sal", "oleo", "farinha", "macarrao", "molho", "cafe"],
  Bebidas: ["leite", "suco", "agua", "refrigerante", "cha", "cafe pronto"],
  Congelados: ["congelado", "hamburguer", "pizza", "lasanha", "frango congelado"],
  Padaria: ["pao", "bolo", "biscoito", "torrada"],
};

const state = {
  items: loadFromStorage(STORAGE_KEY, []),
  history: loadFromStorage(HISTORY_KEY, {}),
  filters: { search: "", category: "all", status: "all" },
};

const refs = {
  itemForm: document.getElementById("item-form"),
  itemName: document.getElementById("item-name"),
  itemQuantity: document.getElementById("item-quantity"),
  itemUnit: document.getElementById("item-unit"),
  itemCategory: document.getElementById("item-category"),
  itemPriority: document.getElementById("item-priority"),
  itemPrice: document.getElementById("item-price"),
  itemNote: document.getElementById("item-note"),
  categoryHint: document.getElementById("category-hint"),
  smartSuggestions: document.getElementById("smart-suggestions"),
  listContainer: document.getElementById("list-container"),
  searchInput: document.getElementById("search-input"),
  filterCategory: document.getElementById("filter-category"),
  filterStatus: document.getElementById("filter-status"),
  statTotal: document.getElementById("stat-total"),
  statDone: document.getElementById("stat-done"),
  statBudget: document.getElementById("stat-budget"),
  statProgress: document.getElementById("stat-progress"),
  shareListBtn: document.getElementById("share-list-btn"),
  resetListBtn: document.getElementById("reset-list-btn"),
  removeDoneBtn: document.getElementById("remove-done-btn"),
  toast: document.getElementById("toast"),
};

let toastTimer = null;

init();

function init() {
  bindEvents();
  renderAll();
}

function bindEvents() {
  refs.itemForm.addEventListener("submit", onItemSubmit);
  refs.itemName.addEventListener("input", updateCategoryHint);
  refs.itemCategory.addEventListener("change", updateCategoryHint);

  refs.searchInput.addEventListener("input", () => {
    state.filters.search = refs.searchInput.value.trim();
    renderList();
  });

  refs.filterCategory.addEventListener("change", () => {
    state.filters.category = refs.filterCategory.value;
    renderList();
  });

  refs.filterStatus.addEventListener("change", () => {
    state.filters.status = refs.filterStatus.value;
    renderList();
  });

  refs.removeDoneBtn.addEventListener("click", removeDoneItems);
  refs.resetListBtn.addEventListener("click", resetList);
  refs.shareListBtn.addEventListener("click", shareList);
}

function onItemSubmit(event) {
  event.preventDefault();

  const formData = new FormData(refs.itemForm);
  const item = createItemFromForm(formData);
  if (!item) {
    showToast("Informe um nome de item valido.");
    return;
  }

  state.items.push(item);
  upsertHistory(item);
  persistItems();
  persistHistory();

  refs.itemForm.reset();
  refs.itemQuantity.value = 1;
  refs.itemPriority.value = "media";
  refs.categoryHint.textContent = "";
  refs.itemName.focus();

  renderAll();
  showToast(`${item.name} adicionado com sucesso.`);
}

function createItemFromForm(formData) {
  const name = String(formData.get("name") || "").trim();
  if (!name) return null;

  const quantity = Math.max(1, Number(formData.get("quantity")) || 1);
  const unit = String(formData.get("unit") || "un");
  const selectedCategory = String(formData.get("category") || "").trim();
  const category = selectedCategory || detectCategory(name) || "Outros";
  const priority = String(formData.get("priority") || "media");
  const price = Math.max(0, Number(formData.get("price")) || 0);
  const note = String(formData.get("note") || "").trim();

  return {
    id: generateId(),
    name,
    quantity,
    unit,
    category,
    priority,
    price,
    note,
    purchased: false,
    createdAt: Date.now(),
  };
}

function renderAll() {
  renderStats();
  renderSuggestions();
  renderList();
}

function renderStats() {
  const total = state.items.length;
  const done = state.items.filter((item) => item.purchased).length;
  const budget = state.items.reduce((sum, item) => sum + (item.price || 0) * (item.quantity || 1), 0);
  const progress = total > 0 ? Math.round((done / total) * 100) : 0;

  refs.statTotal.textContent = String(total);
  refs.statDone.textContent = String(done);
  refs.statBudget.textContent = formatCurrency(budget);
  refs.statProgress.textContent = `${progress}%`;
}

function renderSuggestions() {
  const existingNames = new Set(state.items.map((item) => normalizeText(item.name)));
  const suggestions = [];
  const added = new Set();

  for (const suggestion of BASE_SUGGESTIONS) {
    const key = normalizeText(suggestion.name);
    if (existingNames.has(key) || added.has(key)) continue;
    suggestions.push(suggestion);
    added.add(key);
    if (suggestions.length >= 8) break;
  }

  const historyCandidates = Object.values(state.history)
    .sort((a, b) => (b.count || 0) - (a.count || 0))
    .slice(0, 12);

  for (const entry of historyCandidates) {
    const key = normalizeText(entry.name || "");
    if (!key || existingNames.has(key) || added.has(key)) continue;

    suggestions.push({
      name: entry.name,
      category: entry.category || detectCategory(entry.name) || "Outros",
      unit: entry.unit || "un",
      quantity: 1,
      price: Number(entry.lastPrice) || 0,
    });
    added.add(key);
    if (suggestions.length >= 12) break;
  }

  refs.smartSuggestions.innerHTML = "";

  if (suggestions.length === 0) {
    const message = document.createElement("p");
    message.className = "empty-state";
    message.textContent = "Sem sugestoes por agora. Adicione itens e o sistema aprende com voce.";
    refs.smartSuggestions.appendChild(message);
    return;
  }

  suggestions.forEach((suggestion) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = suggestion.name;
    chip.addEventListener("click", () => quickAddSuggestion(suggestion));
    refs.smartSuggestions.appendChild(chip);
  });
}

function quickAddSuggestion(suggestion) {
  const item = {
    id: generateId(),
    name: suggestion.name,
    quantity: suggestion.quantity || 1,
    unit: suggestion.unit || "un",
    category: suggestion.category || detectCategory(suggestion.name) || "Outros",
    priority: "media",
    price: Number(suggestion.price) || 0,
    note: "",
    purchased: false,
    createdAt: Date.now(),
  };

  state.items.push(item);
  upsertHistory(item);
  persistItems();
  persistHistory();
  renderAll();
  showToast(`${item.name} entrou na lista.`);
}

function renderList() {
  const filtered = applyFilters(state.items);
  const sorted = sortItems(filtered);
  refs.listContainer.innerHTML = "";

  if (sorted.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Sua lista esta vazia ou sem itens neste filtro.";
    refs.listContainer.appendChild(empty);
    return;
  }

  const grouped = groupByCategory(sorted);
  grouped.forEach((items, category) => {
    const block = document.createElement("section");
    block.className = "category-block";

    const title = document.createElement("h3");
    title.className = "category-title";
    title.textContent = `${category} (${items.length})`;
    block.appendChild(title);

    const ul = document.createElement("ul");
    ul.className = "item-list";

    items.forEach((item) => {
      ul.appendChild(renderItemRow(item));
    });

    block.appendChild(ul);
    refs.listContainer.appendChild(block);
  });
}

function renderItemRow(item) {
  const row = document.createElement("li");
  row.className = `item-row ${item.purchased ? "done" : ""}`;

  const check = document.createElement("input");
  check.type = "checkbox";
  check.checked = item.purchased;
  check.setAttribute("aria-label", `Marcar ${item.name} como comprado`);
  check.addEventListener("change", () => toggleItem(item.id));

  const main = document.createElement("div");
  main.className = "item-main";
  const name = document.createElement("p");
  name.className = "item-name";
  name.textContent = item.name;

  const details = [];
  details.push(`${item.quantity} ${item.unit}`);
  if (item.price > 0) details.push(formatCurrency(item.price * item.quantity));
  if (item.note) details.push(item.note);
  const meta = document.createElement("p");
  meta.className = "item-meta";
  meta.textContent = details.join(" | ");

  main.append(name, meta);

  const priority = document.createElement("span");
  priority.className = `priority-tag ${item.priority}`;
  priority.textContent = item.priority;

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "icon-btn";
  remove.textContent = "Excluir";
  remove.addEventListener("click", () => removeItem(item.id));

  row.append(check, main, priority, remove);
  return row;
}

function applyFilters(items) {
  const search = normalizeText(state.filters.search || "");
  const categoryFilter = state.filters.category;
  const statusFilter = state.filters.status;

  return items.filter((item) => {
    if (categoryFilter !== "all" && item.category !== categoryFilter) return false;

    if (statusFilter === "pending" && item.purchased) return false;
    if (statusFilter === "done" && !item.purchased) return false;

    if (!search) return true;

    const haystack = normalizeText(`${item.name} ${item.note || ""} ${item.category}`);
    return haystack.includes(search);
  });
}

function sortItems(items) {
  return [...items].sort((a, b) => {
    if (a.purchased !== b.purchased) return Number(a.purchased) - Number(b.purchased);

    if (a.category !== b.category) {
      return a.category.localeCompare(b.category, "pt-BR");
    }

    const priorityDiff = (PRIORITY_WEIGHT[b.priority] || 0) - (PRIORITY_WEIGHT[a.priority] || 0);
    if (priorityDiff !== 0) return priorityDiff;

    return a.name.localeCompare(b.name, "pt-BR");
  });
}

function groupByCategory(items) {
  return items.reduce((map, item) => {
    const key = item.category || "Outros";
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
    return map;
  }, new Map());
}

function toggleItem(itemId) {
  const item = state.items.find((entry) => entry.id === itemId);
  if (!item) return;

  item.purchased = !item.purchased;
  item.updatedAt = Date.now();
  persistItems();
  renderStats();
  renderList();
}

function removeItem(itemId) {
  const before = state.items.length;
  state.items = state.items.filter((item) => item.id !== itemId);
  if (state.items.length === before) return;

  persistItems();
  renderAll();
  showToast("Item removido da lista.");
}

function removeDoneItems() {
  const doneCount = state.items.filter((item) => item.purchased).length;
  if (doneCount === 0) {
    showToast("Nao ha itens comprados para remover.");
    return;
  }

  state.items = state.items.filter((item) => !item.purchased);
  persistItems();
  renderAll();
  showToast(`${doneCount} item(ns) comprado(s) removido(s).`);
}

function resetList() {
  if (!state.items.length) {
    showToast("A lista ja esta vazia.");
    return;
  }

  const ok = window.confirm("Deseja limpar toda a lista e comecar uma nova?");
  if (!ok) return;

  state.items = [];
  persistItems();
  renderAll();
  showToast("Nova lista iniciada.");
}

async function shareList() {
  if (!state.items.length) {
    showToast("Adicione itens antes de compartilhar.");
    return;
  }

  const text = buildShareText();

  if (navigator.share) {
    try {
      await navigator.share({
        title: "Lista de mercado - Acodexialista",
        text,
      });
      showToast("Lista compartilhada.");
      return;
    } catch (error) {
      if (error && error.name === "AbortError") return;
    }
  }

  try {
    await copyText(text);
    showToast("Lista copiada para a area de transferencia.");
  } catch (_error) {
    showToast("Nao foi possivel compartilhar agora.");
  }
}

function buildShareText() {
  const grouped = groupByCategory(sortItems(state.items));
  const lines = [
    "Acodexialista - Lista de Mercado",
    `Data: ${new Date().toLocaleDateString("pt-BR")}`,
    "",
  ];

  grouped.forEach((items, category) => {
    lines.push(`[${category}]`);
    items.forEach((item) => {
      const done = item.purchased ? "[x]" : "[ ]";
      const pricePart = item.price > 0 ? ` - ${formatCurrency(item.price * item.quantity)}` : "";
      lines.push(`${done} ${item.name} (${item.quantity} ${item.unit})${pricePart}`);
    });
    lines.push("");
  });

  const total = state.items.reduce((sum, item) => sum + (item.price || 0) * (item.quantity || 1), 0);
  lines.push(`Previsao total: ${formatCurrency(total)}`);
  return lines.join("\n");
}

function updateCategoryHint() {
  const name = refs.itemName.value.trim();
  const selectedCategory = refs.itemCategory.value;

  if (!name) {
    refs.categoryHint.textContent = "";
    return;
  }

  if (selectedCategory) {
    refs.categoryHint.textContent = `Categoria manual selecionada: ${selectedCategory}.`;
    return;
  }

  const suggestion = detectCategory(name);
  refs.categoryHint.textContent = suggestion
    ? `Sugestao inteligente: ${suggestion}.`
    : "Sem sugestao automatica. Escolha manualmente se desejar.";
}

function detectCategory(name) {
  const normalized = normalizeText(name);
  if (!normalized) return null;

  for (const [category, keywords] of Object.entries(CATEGORY_KEYWORDS)) {
    if (keywords.some((keyword) => normalized.includes(keyword))) {
      return category;
    }
  }

  return null;
}

function upsertHistory(item) {
  const key = normalizeText(item.name);
  if (!key) return;

  const current = state.history[key] || {
    name: item.name,
    count: 0,
    category: item.category || "Outros",
    lastPrice: 0,
    unit: item.unit || "un",
    updatedAt: Date.now(),
  };

  current.name = item.name;
  current.count = (current.count || 0) + 1;
  current.category = item.category || current.category || "Outros";
  current.unit = item.unit || current.unit || "un";
  if (item.price > 0) current.lastPrice = item.price;
  current.updatedAt = Date.now();

  state.history[key] = current;
}

function persistItems() {
  saveToStorage(STORAGE_KEY, state.items);
}

function persistHistory() {
  saveToStorage(HISTORY_KEY, state.history);
}

function loadFromStorage(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw);
  } catch (_error) {
    return fallback;
  }
}

function saveToStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_error) {
    // Ignora erro de armazenamento (modo privado ou limite localStorage).
  }
}

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function formatCurrency(value) {
  return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }).format(Number(value || 0));
}

function generateId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `item-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function showToast(message) {
  refs.toast.textContent = message;
  refs.toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    refs.toast.classList.remove("visible");
  }, 2400);
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "true");
  helper.style.position = "fixed";
  helper.style.left = "-9999px";
  document.body.appendChild(helper);
  helper.select();
  document.execCommand("copy");
  document.body.removeChild(helper);
}
