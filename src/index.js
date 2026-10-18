// Render entrypoint for the Discord bridge on the discord-self branch.
// Render's existing commands are: npm install / node src/index.js.
const { spawn } = require('node:child_process');
const path = require('node:path');

const discordDir = path.resolve(__dirname, '../discord-self-mcp');
const child = spawn(process.execPath, ['src/launcher.js'], {
  cwd: discordDir,
  env: {
    ...process.env,
    PYTHON_BIN: process.env.PYTHON_BIN || '.venv/bin/python'
  },
  stdio: 'inherit'
});

child.on('error', (error) => {
  console.error('[DiscordLauncher] Unable to start Discord service:', error.message);
  process.exitCode = 1;
});
child.on('exit', (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    if (child.exitCode === null && !child.killed) child.kill(signal);
  });
}
