const path = require('path');

// O Render não preserva o cache padrão de $HOME do Puppeteer entre build/runtime.
// Por isso usamos um cache dentro do próprio projeto, que vai junto no deploy.
if (!process.env.PUPPETEER_CACHE_DIR) {
  process.env.PUPPETEER_CACHE_DIR = path.resolve(__dirname, '.cache', 'puppeteer');
}

console.log(`[Puppeteer] Cache: ${process.env.PUPPETEER_CACHE_DIR}`);

require('./src/server');
