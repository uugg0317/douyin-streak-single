// Test-only loader: execute the same marked source files and order as index.html.
const fs = require('node:fs');
const path = require('node:path');
const staticRoot = path.join(__dirname, '../static');
const expected = ['/static/console-results.js', '/static/console-app.js'];

function loadConsoleSource(html = fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8')) {
  const modules = [];
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const attributes = match[1];
    if (!/\bdata-console-module(?:\s|=|$)/i.test(attributes)) continue;
    const src = attributes.match(/(?:^|\s)src\s*=\s*(["'])(.*?)\1/i)?.[2];
    if (!src || match[2].trim() || /(?:^|\s)(?:async|defer)(?:\s|=|$)/i.test(attributes) || /\btype\s*=\s*["']module["']/i.test(attributes)) {
      throw Error('Console modules must be ordered external classic scripts');
    }
    const url = new URL(src, 'http://console-fixture.invalid');
    if (url.origin !== 'http://console-fixture.invalid' || !expected.includes(url.pathname) || !src.startsWith('/static/')) {
      throw Error('Console module must reference a known local static file');
    }
    modules.push({src, pathname:url.pathname, source:fs.readFileSync(path.join(staticRoot, path.basename(url.pathname)), 'utf8')});
  }
  if (modules.length !== expected.length || modules.some((item, index) => item.pathname !== expected[index])) {
    throw Error('Console result module must load once before the app module');
  }
  return {html, modules, source:modules.map(item => item.source).join('\n;\n')};
}

module.exports = {loadConsoleSource};
