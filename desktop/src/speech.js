const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;

function cellsFromTableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
}

export function spokenText(text) {
  const source = String(text || '')
    .replace(/<think(?:ing)?(?:\s+[^>]*)?>[\s\S]*?(?:<\/think(?:ing)?>|$)/gi, '')
    .replace(/<details[\s\S]*?<\/details>/gi, '')
    .replace(/```[\w-]*\s*[\s\S]*?```/g, ' Code is shown on screen. ')
    .replace(/```[\w-]*\s*[\s\S]*$/g, ' Code is shown on screen. ');

  const lines = source.split(/\r?\n/);
  const output = [];
  for (let index = 0; index < lines.length; index++) {
    if (TABLE_ROW.test(lines[index]) && lines[index + 1] && TABLE_RULE.test(lines[index + 1])) {
      const headers = cellsFromTableRow(lines[index]);
      index += 2;
      const rows = [];
      while (index < lines.length && TABLE_ROW.test(lines[index])) {
        const values = cellsFromTableRow(lines[index]);
        rows.push(values.map((value, cellIndex) => {
          const label = headers[cellIndex];
          return label ? `${label}: ${value}` : value;
        }).filter(Boolean).join('. '));
        index++;
      }
      output.push(...rows);
      index--;
      continue;
    }
    output.push(lines[index]);
  }

  return output.join('\n')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\((?:https?:\/\/[^\s)]+|[^)]*)\)/g, '$1')
    .replace(/https?:\/\/[^\s)<>]+/gi, url => `link available on screen${url.match(/[.,!?;:]+$/)?.[0] || ''}`)
    .replace(/<[^>]*>/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-+*]\s+/gm, '')
    .replace(/^\s*\d+[.)]\s+/gm, '')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/[*_#>|]/g, '')
    .replace(/&/g, ' and ')
    .replace(/\s+/g, ' ')
    .replace(/\s+([,.;:!?])/g, '$1')
    .trim();
}
