import http from 'node:http';

export function startPublicMcpProxy({ publicPort, targetPort }) {
  const server = http.createServer((req, res) => {
    const headers = { ...req.headers };
    const originalContentType = String(headers['content-type'] || '');
    const originalAccept = String(headers.accept || '');

    if (req.method === 'POST' && req.url?.startsWith('/mcp')) {
      headers['content-type'] = 'application/json';
      headers.accept = 'application/json, text/event-stream';
      console.log(`[MCP-PROXY] POST ${req.url} content-type="${originalContentType || '(none)'}" accept="${originalAccept || '(none)'}" -> application/json`);
    }

    headers.host = `127.0.0.1:${targetPort}`;
    delete headers.connection;

    const upstream = http.request({
      hostname: '127.0.0.1',
      port: targetPort,
      method: req.method,
      path: req.url,
      headers
    }, upstreamRes => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    });

    upstream.on('error', error => {
      console.error('[MCP-PROXY] upstream error:', error);
      if (!res.headersSent) {
        res.writeHead(502, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'MCP gateway unavailable' }));
      } else {
        res.end();
      }
    });

    req.pipe(upstream);
  });

  server.listen(publicPort, '0.0.0.0', () => {
    console.log(`[MCP-PROXY] Public compatibility proxy listening on 0.0.0.0:${publicPort}`);
    console.log(`[MCP-PROXY] Forwarding to http://127.0.0.1:${targetPort}`);
  });

  return server;
}
