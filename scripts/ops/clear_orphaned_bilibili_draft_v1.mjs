#!/usr/bin/env node
// Owner-confirmed removal of one orphaned Bilibili dynamic draft.
//
// Dry-run by default: it only reads the composer and prints what it sees.
// With --confirm-clear it clears the editor ONLY when the observed text hash
// matches --expected-caption-sha256 and the draft has no images. It never
// presses publish and never touches a draft that does not match.
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
import { chromium } from 'file:///opt/lighthouse-hub/compositor/node_modules/playwright/index.mjs';

const args = process.argv.slice(2);
const get = (name) => {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : null;
};
const expected = get('--expected-caption-sha256');
const confirmClear = args.includes('--confirm-clear');
const outDir = get('--out') || '/tmp/orphan-draft';
if (!expected || !/^[a-f0-9]{64}$/.test(expected)) {
  console.error(JSON.stringify({ status: 'missing_expected_caption_sha256' }));
  process.exit(2);
}
const root = process.env.HUB_ROOT || '/opt/lighthouse-hub';
const profile = process.env.DRAFT_PROFILE || path.join(root, 'data/browser-profiles/bilibili');
const placeholder = '\u6709\u4ec0\u4e48\u60f3\u548c\u5927\u5bb6\u5206\u4eab\u7684\uff1f';
const clean = (s) => s.replace(/^\u200B/, '').trimEnd();
const digest = (s) => crypto.createHash('sha256').update(s).digest('hex');
await fs.mkdir(outDir, { recursive: true, mode: 0o700 });

const settle = async (page, body) => {
  let last = '';
  for (let i = 0; i < 10; i += 1) {
    await page.waitForTimeout(1000);
    const text = clean(await body.innerText());
    if (text && text === last) return text;
    last = text;
    if (i >= 4 && !text) return text;
  }
  return last;
};

let context;
try {
  context = await chromium.launchPersistentContext(profile, {
    headless: true, chromiumSandbox: true, viewport: { width: 1280, height: 900 },
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
  });
  context.setDefaultTimeout(20000);
  const page = context.pages()[0] || await context.newPage();
  await page.goto('https://t.bilibili.com/', { waitUntil: 'domcontentloaded' });
  await page.locator('a.header-entry-mini').waitFor({ state: 'visible' });
  const body = page.getByPlaceholder(placeholder, { exact: true });
  await body.waitFor({ state: 'visible' });
  const text = await settle(page, body);
  const images = await page.locator('.bili-pics-uploader__item').count();
  const observed = digest(text);
  await page.screenshot({ path: path.join(outDir, 'before.png') });
  const base = { status: 'observed', text_sha256: observed, expected_sha256: expected, images,
                 text_length: text.length, confirm_clear: confirmClear };
  if (observed !== expected) {
    console.log(JSON.stringify({ ...base, status: 'mismatch_refused' }));
    await context.close();
    process.exit(3);
  }
  if (images !== 0) {
    console.log(JSON.stringify({ ...base, status: 'images_present_refused' }));
    await context.close();
    process.exit(4);
  }
  if (!confirmClear) {
    console.log(JSON.stringify({ ...base, status: 'dry_run_ok' }));
    await context.close();
    process.exit(0);
  }
  await body.click();
  await page.keyboard.press('Control+A');
  await page.keyboard.press('Backspace');
  await page.waitForTimeout(800);
  if (clean(await body.innerText()) !== '') {
    await body.click();
    await page.keyboard.press('Control+A');
    await page.keyboard.press('Backspace');
    await page.waitForTimeout(800);
  }
  await page.goto('about:blank');
  await page.goto('https://t.bilibili.com/', { waitUntil: 'domcontentloaded' });
  await page.locator('a.header-entry-mini').waitFor({ state: 'visible' });
  const body2 = page.getByPlaceholder(placeholder, { exact: true });
  await body2.waitFor({ state: 'visible' });
  const after = await settle(page, body2);
  const imagesAfter = await page.locator('.bili-pics-uploader__item').count();
  await page.screenshot({ path: path.join(outDir, 'after.png') });
  const empty = after === '' && imagesAfter === 0;
  console.log(JSON.stringify({ ...base, status: empty ? 'cleared' : 'clear_not_confirmed',
                               after_sha256: digest(after), after_images: imagesAfter }));
  await context.close();
  process.exit(empty ? 0 : 5);
} catch (error) {
  console.log(JSON.stringify({ status: 'error', error: String((error && error.stack) || error) }));
  if (context) await context.close().catch(() => {});
  process.exit(1);
}
