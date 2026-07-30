/**
 * 笔记本只读本地服务（零依赖，直接 node server.js 运行）
 * - 静态服务整个目录（index.html 及所有工具页都能用）
 * - GET /api/notes  扫描 notes/*.md，返回 [{file,title,created,updated,content}]
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const NOTES_DIR = path.join(ROOT, 'notes');
const PORT = process.env.PORT || 8080;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf'
};

function scanDir(dir, region, out) {
  if (!fs.existsSync(dir)) return;
  fs.readdirSync(dir).forEach(f => {
    if (!f.toLowerCase().endsWith('.md')) return;
    const p = path.join(dir, f);
    let content = '';
    try { content = fs.readFileSync(p, 'utf8'); } catch (e) {}
    const stat = fs.statSync(p);
    const m = content.match(/^#\s+(.+)$/m);          // 第一个 # 标题
    const title = m ? m[1].trim() : f.replace(/\.md$/i, '');
    out.push({
      file: region === '其他' ? f : region + '/' + f,
      title: title,
      region: region,
      created: Math.round((stat.birthtimeMs || stat.mtimeMs)),
      updated: Math.round(stat.mtimeMs),
      content: content
    });
  });
}

function listNotes() {
  const out = [];
  if (!fs.existsSync(NOTES_DIR)) return out;
  scanDir(NOTES_DIR, '其他', out);                    // 根目录的 .md 归为「其他」
  fs.readdirSync(NOTES_DIR).forEach(sub => {          // 每个子目录 = 一个地区
    const subPath = path.join(NOTES_DIR, sub);
    try { if (fs.statSync(subPath).isDirectory()) scanDir(subPath, sub, out); } catch (e) {}
  });
  return out.sort((a, b) => b.updated - a.updated);
}

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);

  if (urlPath === '/api/notes') {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(listNotes()));
    return;
  }

  // 静态文件（默认首页 index.html）
  let rel = urlPath === '/' ? '/index.html' : urlPath;
  const filePath = path.normalize(path.join(ROOT, rel));
  if (!filePath.startsWith(ROOT)) { res.writeHead(403); res.end('Forbidden'); return; }

  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }); res.end('404 Not Found'); return; }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(data);
  });
});

server.listen(PORT, () => {
  const url = `http://localhost:${PORT}/notes.html`;
  console.log('笔记本服务已启动：' + url);
  console.log('把 .md 文件放到 notes/ 目录，刷新页面即可看到。按 Ctrl+C 停止。');
  // macOS 自动打开浏览器
  if (process.platform === 'darwin') {
    require('child_process').exec('open "' + url + '"');
  }
});
