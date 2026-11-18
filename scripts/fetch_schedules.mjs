import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
const { chromium } = await import('playwright-core').catch(() => import('playwright'));

const REGIONS = [
  { id: 'kyiv', name: 'м. Київ', url: 'https://www.dtek-kem.com.ua/ua/shutdowns' },
  { id: 'kyiv-region', name: 'Київська обл.', url: 'https://www.dtek-krem.com.ua/ua/shutdowns' },
  { id: 'dnipro', name: 'Дніпропетровська обл.', url: 'https://www.dtek-dnem.com.ua/ua/shutdowns' },
  { id: 'odesa', name: 'Одеська обл.', url: 'https://www.dtek-oem.com.ua/ua/shutdowns' },
];

const OUT_DIR = path.resolve(process.cwd(), 'schedules');
const MAX_RETRY = 3;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readExisting(filePath) {
  try {
    const raw = await readFile(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function extractBalancedJson(html, marker) {
  const idx = html.indexOf(marker);
  if (idx === -1) return null;
  const braceStart = html.indexOf('{', idx + marker.length);
  if (braceStart === -1) return null;

  let depth = 0;
  let inStr = false;
  let escape = false;
  for (let i = braceStart; i < html.length; i++) {
    const ch = html[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === '\\' && inStr) {
      escape = true;
      continue;
    }
    if (ch === '"') {
      inStr = !inStr;
      continue;
    }
    if (!inStr) {
      if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          return JSON.parse(html.slice(braceStart, i + 1));
        }
      }
    }
  }
  return null;
}

async function scrapeRegion(context, region) {
  const outPath = path.join(OUT_DIR, `${region.id}.json`);
  const nowIso = new Date().toISOString();
  let lastError = null;

  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    const page = await context.newPage();
    try {
      console.log(`[${region.id}] Attempt ${attempt}/${MAX_RETRY}: ${region.url}`);
      await page.goto(region.url, { waitUntil: 'networkidle', timeout: 90000 });
      await page.waitForTimeout(1000);

      const html = await page.content();
      let fact = null;
      let preset = null;

      try {
        const fromWindow = await page.evaluate(() => {
          const ds = window.DisconSchedule;
          return ds ? { fact: ds.fact || null, preset: ds.preset || null } : null;
        });
        if (fromWindow) {
          fact = fromWindow.fact;
          preset = fromWindow.preset;
        }
      } catch {}

      if (!fact) fact = extractBalancedJson(html, 'DisconSchedule.fact');
      if (!preset) preset = extractBalancedJson(html, 'DisconSchedule.preset');

      if (!fact && !preset) {
        throw new Error(`No DisconSchedule found in HTML (${html.length} bytes)`);
      }

      const payload = {
        regionId: region.id,
        regionAffiliation: region.name,
        lastUpdated: nowIso,
        fact: fact || {},
        preset: preset || {},
        lastUpdateStatus: {
          status: 'parsed',
          ok: true,
          at: nowIso,
          attempt,
        },
      };

      await writeFile(outPath, JSON.stringify(payload, null, 2) + '\n', 'utf-8');
      console.log(`[${region.id}] Saved schedule successfully (${html.length} bytes HTML).`);
      return;
    } catch (err) {
      lastError = err;
      console.warn(`[${region.id}] Attempt ${attempt} failed: ${err.message}`);
      if (attempt < MAX_RETRY) {
        await sleep(2000 * attempt + Math.floor(Math.random() * 500));
      }
    } finally {
      await page.close();
    }
  }

  console.warn(`[${region.id}] All ${MAX_RETRY} attempts failed.`);
  const existing = await readExisting(outPath);
  if (existing) {
    existing.lastUpdateStatus = {
      status: 'error',
      ok: false,
      message: String(lastError?.message || lastError),
      at: nowIso,
    };
    await writeFile(outPath, JSON.stringify(existing, null, 2) + '\n', 'utf-8');
    console.log(`[${region.id}] Preserved existing fact/preset data.`);
  }
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const launchOpts = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL) {
    launchOpts.channel = process.env.PLAYWRIGHT_CHANNEL;
  }
  const browser = await chromium.launch(launchOpts);

  try {
    const context = await browser.newContext({
      locale: 'uk-UA',
      timezoneId: 'Europe/Kyiv',
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
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
