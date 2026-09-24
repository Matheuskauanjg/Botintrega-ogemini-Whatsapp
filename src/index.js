// Launcher da bridge pessoal do WhatsApp para o Render.
// Mantido neste caminho porque o serviço está configurado com
// Start Command fixo: `node src/index.js`.
const path = require('path');

process.env.PUPPETEER_CACHE_DIR = process.env.PUPPETEER_CACHE_DIR || path.resolve(__dirname, '..', 'whatsapp-personal-render', '.cache', 'puppeteer');

console.log(`[Launcher] PUPPETEER_CACHE_DIR=${process.env.PUPPETEER_CACHE_DIR}`);

require('../whatsapp-personal-render/start.js');
