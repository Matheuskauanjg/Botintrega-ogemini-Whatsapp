const path = require('path');
const { execFileSync } = require('child_process');

// Usa um cache dentro do próprio projeto para não depender de $HOME no Render.
process.env.PUPPETEER_CACHE_DIR = process.env.PUPPETEER_CACHE_DIR || path.resolve(__dirname, '.cache', 'puppeteer');

console.log(`[Puppeteer] Cache: ${process.env.PUPPETEER_CACHE_DIR}`);

function resolveChrome() {
  const puppeteer = require('puppeteer');

  try {
    const executablePath = puppeteer.executablePath();
    console.log(`[Puppeteer] Chrome encontrado em: ${executablePath}`);
    return executablePath;
  } catch (firstError) {
    console.warn(`[Puppeteer] Chrome não encontrado no cache. Instalando em runtime...`);
    console.warn(`[Puppeteer] Motivo: ${firstError.message}`);

    execFileSync(
      'npx',
      ['puppeteer', 'browsers', 'install', 'chrome'],
      {
        cwd: __dirname,
        env: {
          ...process.env,
          PUPPETEER_CACHE_DIR: process.env.PUPPETEER_CACHE_DIR
        },
        stdio: 'inherit'
      }
    );

    const executablePath = puppeteer.executablePath();
    console.log(`[Puppeteer] Chrome instalado em: ${executablePath}`);
    return executablePath;
  }
}

try {
  process.env.PUPPETEER_EXECUTABLE_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || resolveChrome();
  console.log(`[Puppeteer] Executável usado: ${process.env.PUPPETEER_EXECUTABLE_PATH}`);
} catch (error) {
  console.error('[Puppeteer] Não foi possível preparar o Chrome:', error);
  process.exit(1);
}

require('./src/server');
