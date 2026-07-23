'use strict';
/* Ask Chat 렌더러 — 이스케이프 우선 마크다운 라이트.
   원칙: 데이터 유래 문자열은 전부 esc() 를 거친 뒤에만 HTML 이 된다(SHARE §7.3).
   지원: 문단, **굵게**, `코드`, ``` 블록, - 목록, 1. 목록, | 표, ### 제목. */
const ChatRender = (() => {
  function esc(s) {
    return String(s)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  function inline(s) {
    let html = esc(s);
    html = html.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
    html = html.replace(/\*\*([^*\n]+)\*\*/g, (_, b) => `<strong>${b}</strong>`);
    return html;
  }

  function tableHtml(lines) {
    const rows = lines.map(line =>
      line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim()));
    const sepAt = rows.length > 1 && rows[1].every(c => /^:?-{2,}:?$/.test(c)) ? 1 : -1;
    const head = sepAt === 1 ? rows[0] : null;
    const body = sepAt === 1 ? rows.slice(2) : rows;
    let html = '<table>';
    if (head) html += '<thead><tr>' + head.map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead>';
    html += '<tbody>' + body.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') + '</tbody>';
    return html + '</table>';
  }

  function markdown(text) {
    const lines = String(text || '').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const code = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i += 1; }
        i += 1;
        out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i += 1; }
        out.push(tableHtml(rows));
        continue;
      }
      if (/^\s*[-*] /.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*] /.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\s*[-*] /, ''))}</li>`); i += 1;
        }
        out.push(`<ul>${items.join('')}</ul>`);
        continue;
      }
      if (/^\s*\d+\. /.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\. /.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\s*\d+\. /, ''))}</li>`); i += 1;
        }
        out.push(`<ol>${items.join('')}</ol>`);
        continue;
      }
      const heading = line.match(/^(#{1,4}) (.*)$/);
      if (heading) {
        out.push(`<p><strong>${inline(heading[2])}</strong></p>`);
        i += 1;
        continue;
      }
      if (line.trim() === '') { i += 1; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() !== '' &&
             !/^```|^\s*[-*] |^\s*\d+\. |^\s*\|.*\|\s*$|^#{1,4} /.test(lines[i])) {
        para.push(inline(lines[i])); i += 1;
      }
      out.push(`<p>${para.join('<br>')}</p>`);
    }
    return out.join('');
  }

  return { esc, markdown };
})();
