const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
  const token = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../access-token.local.json'), 'utf8')).access_token;
  assert(token, 'Run approved local setup first');
  const browser = await chromium.launch({headless: true,
    ...(process.env.AML_BROWSER_CHANNEL ? {channel: process.env.AML_BROWSER_CHANNEL} : {})});
  const reports = [];
  try {
    for (const [name, viewport] of [['desktop', {width: 1440, height: 1050}], ['mobile', {width: 390, height: 844}]]) {
      const page = await browser.newPage({viewport, deviceScaleFactor: 1});
      page.setDefaultTimeout(300000);
      const errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto((process.env.AML_PREVIEW_URL || 'http://127.0.0.1:8011') + '/ui/');
      await page.locator('#token').fill(token);
      await page.getByRole('button', {name: 'Connect', exact: true}).click();
      await page.waitForFunction(() => document.getElementById('status').textContent === 'Authenticated');
      assert.match(await page.locator('#bq-status').textContent(), /connected/);
      await page.locator('#question').fill('Show transaction count grouped by direction, with one row per direction.');
      const queryResponse = page.waitForResponse(r => r.url().endsWith('/query') && r.request().method() === 'POST');
      await page.getByRole('button', {name: 'Run query', exact: true}).click();
      const result = await (await queryResponse).json();
      assert.equal(result.mode, 'bigquery');
      assert.match(result.job_id, /^aml_/);
      await page.locator('#results').waitFor({state: 'visible'});
      await page.waitForFunction(() => document.querySelectorAll('canvas').length > 0);
      const canvas = await page.locator('canvas').evaluateAll((items) => items.map((canvas) => {
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        let nonblank = 0;
        for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) nonblank++;
        const rect = canvas.getBoundingClientRect();
        return {width: rect.width, height: rect.height, nonblank};
      }));
      assert(canvas.every((c) => c.width > 100 && c.height > 100 && c.nonblank > 1000));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
      await page.screenshot({path: path.join(process.env.AML_SCREENSHOT_DIR || '/tmp', `aml-analytics-${name}.png`), fullPage: true});
      await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
      assert(await page.locator('#results').isHidden());
      assert.equal(await page.locator('#rows').textContent(), '');
      assert.equal(await page.locator('#sql').textContent(), '');
      assert(await page.locator('#run').isDisabled());
      assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
      assert.deepEqual(errors, []);
      reports.push({viewport: name, job_id: result.job_id, rows: result.rows.length, canvas, errors});
      await page.close();
    }
    console.log(JSON.stringify(reports, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
