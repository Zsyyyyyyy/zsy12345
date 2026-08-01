/**
 * 把 notes/ 目录下的 .md 文件打包成静态数据文件 notes-data.js
 * 用法：node build-notes.js
 * 每次新增/修改笔记后运行一次，然后把 notes-data.js 一起提交到 git。
 * 这样页面无需任何后端，静态托管 / 双击 file:// 都能用。
 */
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const NOTES_DIR = path.join(ROOT, 'notes');
const OUT_FILE = path.join(ROOT, 'notes-data.js');

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
  scanDir(NOTES_DIR, '其他', out);                    // 根目录 .md 归「其他」
  fs.readdirSync(NOTES_DIR).forEach(sub => {          // 每个子目录 = 一个地区
    const subPath = path.join(NOTES_DIR, sub);
    try { if (fs.statSync(subPath).isDirectory()) scanDir(subPath, sub, out); } catch (e) {}
  });
  return out.sort((a, b) => b.updated - a.updated);
}

const data = listNotes();
const js = '// 本文件由 build-notes.js 自动生成，请勿手动修改\n' +
  '// 重新生成：node build-notes.js\n' +
  'window.NOTES_DATA = ' + JSON.stringify(data, null, 2) + ';\n';

fs.writeFileSync(OUT_FILE, js, 'utf8');
console.log('已生成 notes-data.js，共 ' + data.length + ' 篇笔记');
data.forEach(n => console.log('  [' + n.region + '] ' + n.title + '  (' + n.file + ')'));

// 防止浏览器/静态托管缓存旧数据：每次构建给 notes.html 里的引用打上版本号
const HTML_FILE = path.join(ROOT, 'notes.html');
if (fs.existsSync(HTML_FILE)) {
  const ts = Date.now();
  let h = fs.readFileSync(HTML_FILE, 'utf8');
  const before = h;
  h = h.replace(/<script src="notes-data\.js(\?v=\d+)?"><\/script>/,
                '<script src="notes-data.js?v=' + ts + '"></script>');
  if (h !== before) {
    fs.writeFileSync(HTML_FILE, h, 'utf8');
    console.log('已更新 notes.html 的 notes-data.js 引用版本号（防缓存）');
  }
}
