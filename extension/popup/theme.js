// Use the browser's popup palette, not the website's theme. Default/system themes
// without explicit colors fall back to CSS color-scheme, including live changes.
(async () => {
  const { id: windowId } = await browser.windows.getCurrent();
  const style = document.documentElement.style;
  function apply(theme) {
    const colors = theme.colors || {};
    // Keep background/text paired: a partial custom theme must not make text unreadable.
    const pair = colors.popup && colors.popup_text ? [colors.popup, colors.popup_text]
      : colors.toolbar && colors.toolbar_text ? [colors.toolbar, colors.toolbar_text] : [];
    for (const [index, property] of ['--surface', '--on-surface'].entries()) {
      const value = Array.isArray(pair[index]) ? `rgb(${pair[index].join(',')})` : pair[index];
      if (value && CSS.supports('color', value)) style.setProperty(property, value);
      else style.removeProperty(property);
    }
    const scheme = theme.properties?.color_scheme;
    if (scheme === 'light' || scheme === 'dark') style.colorScheme = scheme;
    else style.removeProperty('color-scheme');
  }
  // Re-read this window: a theme update can be global or specific to another window.
  const refresh = async () => apply(await browser.theme.getCurrent(windowId));
  browser.theme.onUpdated.addListener(({ windowId: changed }) => {
    if (changed == null || changed === windowId) refresh().catch(() => {});
  });
  await refresh();
})().catch(() => {}); // CSS still follows light/dark when the theme API is unavailable.
