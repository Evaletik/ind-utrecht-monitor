// Read-only inspection of the visible IND date picker.
// Adds harmless data-ind-monitor-* attributes to navigation controls so that
// Playwright can click a REAL UI arrow (never confirms/submits a reservation).
() => {
  const names = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
    "augustus", "september", "oktober", "november", "december"];
  const english = ["january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december"];
  const norm = v => String(v || "").trim().toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  const shown = e => {
    if (!e || !e.getBoundingClientRect) return false;
    const box = e.getBoundingClientRect();
    const css = getComputedStyle(e);
    return box.width > 0 && box.height > 0 && css.display !== "none" &&
      css.visibility !== "hidden" && css.opacity !== "0";
  };
  const directLeaf = e => {
    const value = (e.textContent || "").trim();
    if (!value) return false;
    return !Array.from(e.children).some(c => (c.textContent || "").trim() === value);
  };
  const all = Array.from(document.body.querySelectorAll("*"));
  const monthEls = all.filter(e => shown(e) && directLeaf(e) &&
    [...names, ...english].includes(norm(e.textContent)));
  const weekdays = ["ma", "di", "wo", "do", "vr", "za", "zo"];
  const weekdaysEnglish = ["mo", "tu", "we", "th", "fr", "sa", "su"];
  const containsWeekdays = txt => {
    const s = " " + norm(txt).replace(/[^a-z]+/g, " ").trim() + " ";
    const hits = words => words.filter(w => s.includes(" " + w + " ")).length;
    return hits(weekdays) >= 6 || hits(weekdaysEnglish) >= 6 ||
      /\bmon\b/.test(s) && /\btue\b/.test(s) && /\bwed\b/.test(s);
  };
  const numericLeafEls = parent => Array.from(parent.querySelectorAll("*"))
    .filter(e => shown(e) && directLeaf(e) && /^(?:[1-9]|[12][0-9]|3[01])$/.test((e.textContent || "").trim()));

  let choice = null;
  for (const monthEl of monthEls) {
    let container = monthEl;
    for (let depth = 0; depth < 9 && container && container !== document.body; depth++, container = container.parentElement) {
      const txt = container.innerText || "";
      if (!containsWeekdays(txt) || !/\b20\d{2}\b/.test(txt)) continue;
      const count = numericLeafEls(container).length;
      if (count >= 28 && count <= 70) {
        const order = [...names, ...english].indexOf(norm(monthEl.textContent));
        const month = order < 12 ? order + 1 : order - 11;
        const years = txt.match(/\b20\d{2}\b/g) || [];
        const year = Number(years[0]);
        if (year > 2020 && year < 2100) {
          const candidate = {root: container, monthEl, month, year, count, depth};
          if (!choice || candidate.count < choice.count ||
            (candidate.count === choice.count && candidate.depth < choice.depth)) choice = candidate;
          break;
        }
      }
    }
  }
  if (!choice) return {
    ok: false,
    reason: "Не знайдено зрозумілий календар (місяць + рік + дні тижня + ≥28 комірок).",
    visibleMonths: monthEls.map(e => (e.textContent || "").trim()).slice(0, 8),
    snippet: (document.body.innerText || "").slice(0, 450)
  };

  const {root, monthEl, month, year} = choice;
  root.setAttribute("data-ind-monitor-calendar", "1");
  const green = rgb => {
    const m = rgb.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
    if (!m) return false;
    const [r, g, b] = m.slice(1).map(Number);
    // IND's available-day tiles are turquoise/mint green.
    return r >= 45 && r <= 185 && g >= 145 && g <= 245 && b >= 95 && b <= 225 &&
      g > r + 25 && g > b + 5;
  };
  const cells = numericLeafEls(root);
  const available = new Set();
  const details = [];
  for (const el of cells) {
    const day = Number((el.textContent || "").trim());
    let coloredNode = null;
    let at = el;
    while (at && at !== root) {
      const bg = getComputedStyle(at).backgroundColor;
      if (green(bg)) {coloredNode = at; break;}
      at = at.parentElement;
    }
    if (!coloredNode) continue;
    const disabled = !!coloredNode.closest('[disabled], [aria-disabled="true"], .disabled, .other-month, .outside-month');
    if (!disabled) available.add(day);
    details.push({day, color: getComputedStyle(coloredNode).backgroundColor, disabled});
  }

  // Identify the left/right arrows geometrically relative to the MONTH caption.
  // Do not synthesize DOM events; the Python driver will click the tagged control.
  for (const old of root.querySelectorAll('[data-ind-monitor-nav]')) old.removeAttribute('data-ind-monitor-nav');
  const monthBox = monthEl.getBoundingClientRect();
  const arrows = Array.from(root.querySelectorAll('*')).filter(e => {
    if (!shown(e)) return false;
    const b = e.getBoundingClientRect();
    if (b.width > 125 || b.height > 110 || Math.abs(b.top + b.height / 2 - (monthBox.top + monthBox.height / 2)) > 70) return false;
    const t = (e.textContent || "").trim();
    const title = norm((e.getAttribute("aria-label") || "") + " " + (e.getAttribute("title") || ""));
    const cls = norm(typeof e.className === 'string' ? e.className : '');
    return /^[<>‹›«»❮❯←→]$/.test(t) ||
      /\b(next|previous|prev|volgende|vorige|chevron-right|chevron-left|arrow-right|arrow-left)\b/.test(title) ||
      /\b(next|previous|prev|volgende|vorige|chevron-right|chevron-left|arrow-right|arrow-left)\b/.test(cls);
  });
  const midX = monthBox.left + monthBox.width / 2;
  const navigation = {};
  for (const direction of ["prev", "next"]) {
    const onSide = arrows.filter(e => {
      const b = e.getBoundingClientRect();
      return direction === "next" ? b.left > midX + 25 : b.right < midX - 25;
    });
    onSide.sort((a, b) => {
      const rectA = a.getBoundingClientRect(), rectB = b.getBoundingClientRect();
      // Further right for next, further left for previous; prefer semantic controls.
      const av = (direction === 'next' ? rectA.left : -rectA.right) +
        (a.matches('button, [role="button"]') ? 40 : 0);
      const bv = (direction === 'next' ? rectB.left : -rectB.right) +
        (b.matches('button, [role="button"]') ? 40 : 0);
      return bv - av;
    });
    if (onSide.length) {
      const chosen = onSide[0].closest('button, [role="button"], a') || onSide[0];
      chosen.setAttribute('data-ind-monitor-nav', direction);
      navigation[direction] = true;
    }
  }
  return {
    ok: true, month, year, dayTiles: cells.length,
    availableDays: Array.from(available).sort((a,b) => a-b),
    navigation, greenDetails: details.slice(0, 50)
  };
}
