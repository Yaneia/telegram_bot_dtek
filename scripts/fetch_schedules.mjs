import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const REGIONS = [
  { id: 'kyiv', name: 'м. Київ', url: 'https://www.dtek-kem.com.ua/ua/shutdowns' },
  { id: 'kyiv-region', name: 'Київська обл.', url: 'https://www.dtek-krem.com.ua/ua/shutdowns' },
  { id: 'dnipro', name: 'Дніпропетровська обл.', url: 'https://www.dtek-dnem.com.ua/ua/shutdowns' },
  { id: 'odesa', name: 'Одеська обл.', url: 'https://www.dtek-oem.com.ua/ua/shutdowns' },
];

const OUT_DIR = path.resolve(process.cwd(), 'schedules');

async function readExisting(filePath) {
  try {
    const raw = await readFile(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

async function scrapeRegion(context, region) {
  const page = await context.newPage();
  const outPath = path.join(OUT_DIR, `${region.id}.json`);
  const nowIso = new Date().toISOString();

  try {
    console.log(`[${region.id}] Navigating to ${region.url}...`);
    await page.goto(region.url, { waitUntil: 'domcontentloaded', timeout: 45000 });

    // Wait for Incapsula JS challenge (if any) to resolve and DisconSchedule to populate
    await page.waitForFunction(
      () => typeof window.DisconSchedule !== 'undefined' && (window.DisconSchedule.fact || window.DisconSchedule.preset),
      { timeout: 30000 }
    );

    const extracted = await page.evaluate(() => {
      const ds = window.DisconSchedule || {};
      return {
        fact: ds.fact || null,
        preset: ds.preset || null,
      };
    });

    if (!extracted.fact && !extracted.preset) {
      throw new Error('DisconSchedule.fact and preset are both empty');
    }

    const payload = {
      regionId: region.id,
      regionAffiliation: region.name,
      lastUpdated: nowIso,
      fact: extracted.fact || {},
      preset: extracted.preset || {},
      lastUpdateStatus: {
        status: 'parsed',
        ok: true,
        at: nowIso,
      },
    };

    await writeFile(outPath, JSON.stringify(payload, null, 2) + '\n', 'utf-8');
    console.log(`[${region.id}] Saved schedule successfully.`);
  } catch (err) {
    console.warn(`[${region.id}] Scrape failed: ${err.message}`);
    const existing = await readExisting(outPath);
    if (existing) {
      existing.lastUpdateStatus = {
        status: 'error',
        ok: false,
        message: String(err.message || err),
        at: nowIso,
      };
      await writeFile(outPath, JSON.stringify(existing, null, 2) + '\n', 'utf-8');
      console.log(`[${region.id}] Preserved existing fact/preset data.`);
    }
  } finally {
    await page.close();
  }
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
  });

  try {
    const context = await browser.newContext({
      locale: 'uk-UA',
      timezoneId: 'Europe/Kyiv',
      userAgent:
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
      viewport: { width: 1280, height: 800 },
    });

    for (const region of REGIONS) {
      await scrapeRegion(context, region);
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error('Fatal scraper error:', err);
  process.exit(1);
});
