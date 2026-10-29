async (page) => {
  const config = await page.evaluate(() => window.DW_CAPTURE_OPTIONS || {});
  const output = config.outputDirectory || "output/playwright/images";
  const catalog = await page.evaluate(() => window.DW_PROTOTYPE.screens);
  const selected = catalog.filter(screen => (!config.group || screen.group === config.group) && (!config.ids || config.ids.includes(screen.id)));
  const deviceScaleFactor = config.deviceScaleFactor || 1;
  if (![1, 2].includes(deviceScaleFactor)) throw new Error("不支持的截图像素密度");
  let cdp = null, highResolutionContext = null, lastViewport = null;
  const errors = [];
  const onError = error => errors.push(error.message || String(error));
  const results = [];
  try {
  if (deviceScaleFactor === 2) {
    const originalUrl = page.url();
    highResolutionContext = await page.context().browser().newContext({
      deviceScaleFactor: 2, viewport: selected[0].viewport
    });
    page = await highResolutionContext.newPage();
    page.on("pageerror", onError);
    await page.goto(originalUrl);
    await page.waitForFunction(() => window.DW_PROTOTYPE && window.DW_READY);
    cdp = await page.context().newCDPSession(page);
  } else {
    page.on("pageerror", onError);
  }
  await page.emulateMedia({colorScheme: "dark"});
  for (const screen of selected) {
    await page.setViewportSize(screen.viewport);
    lastViewport = screen.viewport;
    if (cdp) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width: screen.viewport.width, height: screen.viewport.height,
        deviceScaleFactor, mobile: false,
        screenWidth: screen.viewport.width, screenHeight: screen.viewport.height
      });
    }
    await page.evaluate(id => window.DW_PROTOTYPE.renderScreen(id), screen.id);
    await page.waitForFunction(id => window.DW_READY === id, screen.id);
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const issues = await page.evaluate(() => {
      const app = document.querySelector("#stage .app-window");
      const bounds = app.getBoundingClientRect();
      const known = new Set(window.DW_PROTOTYPE.screens.map(s => s.id));
      const brokenRoutes = [...app.querySelectorAll("[data-go]")].filter(e => !known.has(e.dataset.go)).map(e => e.dataset.go);
      const outside = [...app.querySelectorAll(".workbar,.modal,.modal-footer,.installer-window,.native-dialog,.capture-float,.record-float")].filter(e => {
        const r = e.getBoundingClientRect();
        return r.width && (r.left < bounds.left - 1 || r.right > bounds.right + 1 || r.top < bounds.top - 1 || r.bottom > bounds.bottom + 1);
      }).map(e => e.className);
      const clippedButtons = [...app.querySelectorAll("button")].filter(e => e.clientWidth > 0 && getComputedStyle(e).whiteSpace === "nowrap" && e.scrollWidth > e.clientWidth + 3).map(e => e.innerText.trim());
      const unnamedFields = [...app.querySelectorAll("input,select,textarea")].filter(e => e.getBoundingClientRect().width > 0 && !e.getAttribute("aria-label") && !e.closest("label")?.innerText.trim()).map(e => e.outerHTML.slice(0,100));
      const duplicateIds = [...app.querySelectorAll("[id]")].map(e => e.id).filter((id,i,a)=>a.indexOf(id)!==i);
      const screen = window.DW_PROTOTYPE.screens.find(s=>s.id===app.dataset.screen);
      const expected = screen.action ? Object.keys(window.DW_CATALOG.actionModels[screen.action].parameters) : [];
      const actual = [...app.querySelectorAll("[data-parameter]")].map(e=>e.dataset.parameter);
      const missingParameters = expected.filter(name=>!actual.includes(name));
      const completed = [...app.querySelectorAll("[data-ve-complete]")].map(e=>e.dataset.veComplete);
      const completionMismatch = screen.completion && (completed.length!==1 || completed[0]!==screen.completion) ? completed.concat("expected:"+screen.completion) : [];


      const scrollbarTheme = [];
      const luminance = color => {
        const rgb = color.match(/[\d.]+/g)?.slice(0, 3).map(Number);
        if (!rgb || rgb.length !== 3) return NaN;
        return rgb.map(value => {
          const channel = value / 255;
          return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
        }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
      };
      const nativeElements = [document.documentElement, document.body, ...document.querySelectorAll(".review-nav,.review-main,.review-spec"), app, ...app.querySelectorAll("*")];
      for (const element of nativeElements) {
        const style = getComputedStyle(element);
        if (![document.documentElement, document.body, app].includes(element) && !element.matches("input,select,textarea") && !/auto|scroll/.test(style.overflowX + " " + style.overflowY)) continue;
        const dark = !!element.closest(".app-window.dark");
        const label = element.id || element.className || element.tagName;
        if (style.colorScheme !== (dark ? "dark" : "light")) scrollbarTheme.push(label + ": native color scheme");
        const colors = style.scrollbarColor.match(/rgba?\([^)]+\)/g);
        if (!colors || colors.length !== 2) {
          scrollbarTheme.push(label + ": missing scrollbar colors");
          continue;
        }
        const thumb = luminance(colors[0]), track = luminance(colors[1]);
        const contrast = (Math.max(thumb, track) + .05) / (Math.min(thumb, track) + .05);
        if (!Number.isFinite(contrast) || contrast < 3 || (dark ? track > .2 : track < .8 || thumb < .15)) scrollbarTheme.push(label + ": scrollbar contrast or theme");
      }

      return { brokenRoutes, outside, clippedButtons, unnamedFields, duplicateIds, missingParameters, completionMismatch, scrollbarTheme, width: bounds.width, height: bounds.height };
    });
    const actualScale = await page.evaluate(() => window.devicePixelRatio);
    if (actualScale !== deviceScaleFactor) throw new Error("实际设备像素密度与截图配置不一致");
    const path = config.checkOnly ? null : output + "/" + screen.image;
    if (path) await page.screenshot({path, scale:deviceScaleFactor === 2 ? "device" : "css", animations:"disabled"});
    results.push({id:screen.id,issues});
  }
  } finally {
    await page.emulateMedia({colorScheme: null}).catch(() => {});
    page.removeListener("pageerror", onError);
    try {
      if (cdp) {
        try {
          if (lastViewport) await cdp.send("Emulation.setDeviceMetricsOverride", {
            width: lastViewport.width, height: lastViewport.height,
            deviceScaleFactor: 1, mobile: false,
            screenWidth: lastViewport.width, screenHeight: lastViewport.height
          });
        } finally {
          await cdp.detach();
        }
      }
    } finally {
      if (highResolutionContext) await highResolutionContext.close();
    }
  }
  return {group:config.group || "selection",count:results.length,deviceScaleFactor,pageErrors:errors,screens:results};
}
