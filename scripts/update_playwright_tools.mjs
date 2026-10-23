// Capture @playwright/mcp's core tool definitions into src/kitsune/playwright_tools.json.
//
//   node scripts/update_playwright_tools.mjs           # latest @playwright/mcp
//   node scripts/update_playwright_tools.mjs 0.0.70    # a pinned version
//
// Starts Playwright MCP with its default capabilities over stdio, reads tools/list and writes
// the tools verbatim; Kitsune publishes them as its core tools. A tool added or removed
// upstream then needs an adapter in src/kitsune/playwright_tools.py (register() fails until
// the two match), and tests/tool_schema_sizes.json needs `uv run python scripts/tool_schema_size.py --update`.
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const version = process.argv[2] || 'latest';
const output = fileURLToPath(new URL('../src/kitsune/playwright_tools.json', import.meta.url));
const server = spawn('npx', ['-y', `@playwright/mcp@${version}`, '--headless'], { stdio: ['pipe', 'pipe', 'inherit'] });
const send = message => server.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
let buffer = '';
let serverInfo;

server.stdout.on('data', chunk => {
  buffer += chunk;
  let end;
  while ((end = buffer.indexOf('\n')) >= 0) {
    const message = JSON.parse(buffer.slice(0, end));
    buffer = buffer.slice(end + 1);
    if (message.id === 1) {
      serverInfo = message.result.serverInfo;
      send({ method: 'notifications/initialized' });
      send({ id: 2, method: 'tools/list' });
    } else if (message.id === 2) {
      const source = `@playwright/mcp ${serverInfo.version} tools/list (--headless, default caps)`;
      writeFileSync(output, JSON.stringify({ source, tools: message.result.tools }, null, 1) + '\n');
      console.log(`wrote ${message.result.tools.length} tools from ${source}`);
      server.kill();
      process.exit(0);
    }
  }
});
server.on('exit', code => {
  console.error(`@playwright/mcp exited (${code}) before listing its tools`);
  process.exit(1);
});
send({ id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {},
  clientInfo: { name: 'kitsune-update-playwright-tools', version: '1' } } });
