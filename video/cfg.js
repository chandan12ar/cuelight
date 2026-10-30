window.V_CONFIG = {
  accent: '#1f7a4d',
  root: 'body',
  ready: '#totals > *',
  resolvers: {
    // card:<heading> -> the Insights/Spend card whose h3 starts with that heading
    card: (want, root) => [...document.querySelectorAll('main section.view:not([hidden]) section')]
      .find(s => { const h = s.querySelector(':scope > h3, :scope > header h3, h3'); return h && h.textContent.trim().toLowerCase().startsWith(want.toLowerCase()); }),
    // h:<text> -> a visible h3/h4 whose text starts with that heading
    h: (want) => [...document.querySelectorAll('main section.view:not([hidden]) h3, main section.view:not([hidden]) h4')]
      .find(e => e.textContent.trim().toLowerCase().startsWith(want.toLowerCase())),
    // node:<agent> -> a graph node by its title
    node: (want) => [...document.querySelectorAll('#graph g.node')].find(g => g.textContent.toLowerCase().startsWith(want.toLowerCase()))
  }
};
